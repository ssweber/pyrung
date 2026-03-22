"""Click validation findings and suggestion helpers."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Literal

if TYPE_CHECKING:
    from pyrung.click.tag_map import TagMap
    from pyrung.core.validation.walker import OperandFact

ValidationMode = Literal["warn", "strict"]
FindingSeverity = Literal["error", "warning", "hint"]

# ---------------------------------------------------------------------------
# Finding codes
# ---------------------------------------------------------------------------

CLK_PTR_CONTEXT_ONLY_COPY = "CLK_PTR_CONTEXT_ONLY_COPY"
CLK_PTR_POINTER_MUST_BE_DS = "CLK_PTR_POINTER_MUST_BE_DS"
CLK_PTR_EXPR_NOT_ALLOWED = "CLK_PTR_EXPR_NOT_ALLOWED"
CLK_EXPR_ONLY_IN_CALC = "CLK_EXPR_ONLY_IN_CALC"
CLK_TILDE_BOOL_CONTACT_ONLY = "CLK_TILDE_BOOL_CONTACT_ONLY"
CLK_INT_TRUTHINESS_EXPLICIT_COMPARE_REQUIRED = "CLK_INT_TRUTHINESS_EXPLICIT_COMPARE_REQUIRED"
CLK_INDIRECT_BLOCK_RANGE_NOT_ALLOWED = "CLK_INDIRECT_BLOCK_RANGE_NOT_ALLOWED"
CLK_PTR_DS_UNVERIFIED = "CLK_PTR_DS_UNVERIFIED"
CLK_FUNCTION_CALL_NOT_PORTABLE = "CLK_FUNCTION_CALL_NOT_PORTABLE"
CLK_CALC_MODE_MIXED = "CLK_CALC_MODE_MIXED"

CLK_PROFILE_UNAVAILABLE = "CLK_PROFILE_UNAVAILABLE"
CLK_BANK_UNRESOLVED = "CLK_BANK_UNRESOLVED"
CLK_BANK_NOT_WRITABLE = "CLK_BANK_NOT_WRITABLE"
CLK_BANK_WRONG_ROLE = "CLK_BANK_WRONG_ROLE"
CLK_COPY_BANK_INCOMPATIBLE = "CLK_COPY_BANK_INCOMPATIBLE"
CLK_COPY_CONVERTER_INCOMPATIBLE = "CLK_COPY_CONVERTER_INCOMPATIBLE"
CLK_PACK_TEXT_BANK_INCOMPATIBLE = "CLK_PACK_TEXT_BANK_INCOMPATIBLE"
CLK_DRUM_TIME_PRESET_LITERAL_REQUIRED = "CLK_DRUM_TIME_PRESET_LITERAL_REQUIRED"
CLK_IMMEDIATE_CONTEXT_NOT_ALLOWED = "CLK_IMMEDIATE_CONTEXT_NOT_ALLOWED"
CLK_IMMEDIATE_EDGE_CONTACT_NOT_ALLOWED = "CLK_IMMEDIATE_EDGE_CONTACT_NOT_ALLOWED"
CLK_IMMEDIATE_COIL_TARGET_MUST_BE_Y = "CLK_IMMEDIATE_COIL_TARGET_MUST_BE_Y"
CLK_IMMEDIATE_RANGE_MUST_BE_CONTIGUOUS = "CLK_IMMEDIATE_RANGE_MUST_BE_CONTIGUOUS"


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


def _route_severity(code: str, mode: ValidationMode) -> FindingSeverity:
    if mode == "strict":
        return "error"
    if code == CLK_PROFILE_UNAVAILABLE:
        return "warning"
    return "hint"


def _build_suggestion(code: str, fact: OperandFact | None, tag_map: TagMap) -> str:
    """Build a context-aware suggestion string for a finding code."""
    meta = {} if fact is None else fact.metadata

    if code == CLK_PTR_CONTEXT_ONLY_COPY:
        block_name = str(meta.get("block_name", ""))
        pointer_name = str(meta.get("pointer_name", ""))
        if block_name and pointer_name:
            return (
                f"Pointer {block_name}[{pointer_name}] can only be used inside copy(). "
                "Use a direct tag reference here instead."
            )
        return "Use direct tag addressing in this context; keep pointer usage in copy() only."

    if code == CLK_PTR_POINTER_MUST_BE_DS:
        from .resolve import _resolve_pointer_memory_type

        pointer_name = str(meta.get("pointer_name", ""))
        resolved_type = (
            _resolve_pointer_memory_type(pointer_name, tag_map) if pointer_name else None
        )
        if pointer_name and resolved_type:
            return (
                f"Pointer '{pointer_name}' is mapped to {resolved_type} memory. "
                "Remap it to a DS address so Click hardware can use it as a pointer."
            )
        return "Use a DS tag as the pointer source for copy() addressing."

    if code == CLK_PTR_DS_UNVERIFIED:
        pointer_name = str(meta.get("pointer_name", ""))
        if pointer_name:
            return (
                f"Pointer '{pointer_name}' is not in the tag map - cannot verify it is DS. "
                f"Map it to a DS address: {pointer_name}.map_to(ds[N])"
            )
        return "Use a DS tag as the pointer source for copy() addressing."

    if code == CLK_PTR_EXPR_NOT_ALLOWED:
        block_name = str(meta.get("block_name", ""))
        expr_dsl = str(meta.get("expr_dsl", ""))
        if block_name and expr_dsl:
            block_entry = tag_map.block_entry_by_name(block_name)
            if block_entry is not None:
                try:
                    offset = tag_map.offset_for(block_entry.logical)
                    return (
                        f"Click cannot compute {block_name}[{expr_dsl}] at runtime. "
                        f"Store the index in a DS tag and use copy(): "
                        f"calc({expr_dsl} + {offset}, Ptr); copy({block_name}[Ptr], dest)"
                    )
                except (KeyError, ValueError):
                    pass
            return (
                f"Click cannot compute {block_name}[{expr_dsl}] at runtime. "
                "Pre-compute the index with calc() into a DS pointer tag, "
                "then use block[Ptr]."
            )
        return "Replace computed pointer arithmetic with DS pointer tag updated separately."

    if code == CLK_EXPR_ONLY_IN_CALC:
        expr_dsl = str(meta.get("expr_dsl", ""))
        if expr_dsl:
            return (
                f"Expression '{expr_dsl}' cannot be used directly here. "
                f"Move it into calc({expr_dsl}, temp) and use temp in this context."
            )
        return "Move expression into calc(expr, temp) and use temp in this context."

    if code == CLK_TILDE_BOOL_CONTACT_ONLY:
        expr_dsl = str(meta.get("expr_dsl", ""))
        if expr_dsl:
            return (
                f"Expression '{expr_dsl}' uses `~`. Click portability reserves `~` for BOOL "
                "contact inversion in rung conditions. Rewrite with explicit math or masking."
            )
        return (
            "Click portability reserves `~` for BOOL contact inversion in rung conditions. "
            "Rewrite bitwise inversion with explicit math or masking."
        )

    if code == CLK_INT_TRUTHINESS_EXPLICIT_COMPARE_REQUIRED:
        return (
            "Click portability prefers explicit INT comparisons in conditions. "
            "Rewrite as Rung(tag != 0) (or Rung(tag == 0) for inverted intent)."
        )

    if code == CLK_INDIRECT_BLOCK_RANGE_NOT_ALLOWED:
        block_name = str(meta.get("block_name", ""))
        if block_name:
            return (
                f"Block '{block_name}' uses computed range bounds. "
                "Use a fixed BlockRange with literal start/end addresses instead."
            )
        return "Use a fixed BlockRange with literal start/end addresses for block copy."

    if code == CLK_FUNCTION_CALL_NOT_PORTABLE:
        return (
            "Replace run_function/run_enabled_function with Click-portable instructions "
            "(copy/calc/timer/counter/send/receive)."
        )

    if code == CLK_CALC_MODE_MIXED:
        return (
            "Split mixed calc math into separate decimal and WORD-only calc() steps, "
            "or convert through an intermediate tag so each calc() stays one family."
        )

    if code == CLK_COPY_CONVERTER_INCOMPATIBLE:
        return (
            "Converters require specific bank types: "
            "to_text/to_binary need a numeric source and TXT destination; "
            "to_value/to_ascii need a TXT source and numeric destination."
        )

    if code == CLK_IMMEDIATE_CONTEXT_NOT_ALLOWED:
        return "Use immediate(...) only in rung contacts and out()/latch()/reset() coil targets."

    if code == CLK_IMMEDIATE_EDGE_CONTACT_NOT_ALLOWED:
        return "Use rise(tag)/fall(tag) without immediate(...), or use a plain immediate contact."

    if code == CLK_IMMEDIATE_COIL_TARGET_MUST_BE_Y:
        return "Map immediate coil targets to Y bank addresses only."

    if code == CLK_IMMEDIATE_RANGE_MUST_BE_CONTIGUOUS:
        return "Map immediate-wrapped coil ranges to contiguous Y addresses (Ynnn..Ymmm)."

    return ""


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
    "CLK_PROFILE_UNAVAILABLE",
    "CLK_BANK_UNRESOLVED",
    "CLK_BANK_NOT_WRITABLE",
    "CLK_BANK_WRONG_ROLE",
    "CLK_COPY_BANK_INCOMPATIBLE",
    "CLK_COPY_CONVERTER_INCOMPATIBLE",
    "CLK_PACK_TEXT_BANK_INCOMPATIBLE",
    "CLK_DRUM_TIME_PRESET_LITERAL_REQUIRED",
    "CLK_IMMEDIATE_CONTEXT_NOT_ALLOWED",
    "CLK_IMMEDIATE_EDGE_CONTACT_NOT_ALLOWED",
    "CLK_IMMEDIATE_COIL_TARGET_MUST_BE_Y",
    "CLK_IMMEDIATE_RANGE_MUST_BE_CONTIGUOUS",
    "ClickFinding",
    "ClickValidationReport",
]
