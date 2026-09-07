"""Indirect-pointer domain validation for pyrung programs.

Detects exact indirect dereference sites where the pointer tag's effective
default resolves below the indexed block's first valid address, or its complete
domain contains other concrete values outside the block.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from pyrung.core.condition import CompareEq, CompareGt, CompareLt
from pyrung.core.validation._common import (
    _conjunction_satisfiable,
    compact_location,
)
from pyrung.core.validation.display import FindingDisplay, Frame, _FindingTextMixin
from pyrung.core.validation.render import caret_of
from pyrung.core.validation.severity import Severity
from pyrung.core.validation.walker import OperandFact, ProgramLocation, walk_program

if TYPE_CHECKING:
    from pyrung.core.program import Program
    from pyrung.core.validation.context import ValidationContext


PTR_DEFAULT_BEFORE_BLOCK_START = "PTR_DEFAULT_BEFORE_BLOCK_START"
PTR_MAY_ESCAPE_BLOCK = "PTR_MAY_ESCAPE_BLOCK"
PTR_UNGUARDED_ACCESS = "PTR_UNGUARDED_ACCESS"


@dataclass(frozen=True)
class PointerDefaultFinding(_FindingTextMixin):
    """An unsafe default or closed domain at an indirect block dereference."""

    code: str
    target_name: str
    block_name: str
    pointer_name: str
    pointer_default: int
    block_start: int
    block_end: int
    sites: tuple[ProgramLocation, ...]
    display: FindingDisplay
    severity: Severity = "warning"
    bad_values: tuple[int, ...] = ()

    @property
    def message(self) -> str:
        return self.display.as_text()


@dataclass(frozen=True)
class PointerDefaultReport:
    findings: tuple[PointerDefaultFinding, ...]

    def summary(self) -> str:
        if not self.findings:
            return "No pointer default violations."
        return f"{len(self.findings)} pointer default violation(s)."


def _location_frame(
    site: ProgramLocation, code: str, span: tuple[int, int] | None, label: str
) -> Frame:
    """A diagnostic frame for a walker dereference site."""
    return Frame(
        location=compact_location(site.scope, site.subroutine, site.rung_index, site.branch_path),
        lines=(code,),
        caret=(0, span[0], span[1]) if span else None,
        caret_label=label if span else "",
    )


def _grouped_pointer_facts(program: Program) -> dict[tuple[str, str], list[OperandFact]]:
    """Collect exact indirect dereference facts by block and pointer."""
    grouped: dict[tuple[str, str], list[OperandFact]] = {}
    facts = walk_program(program)

    for fact in facts.operands:
        if fact.value_kind != "indirect_ref":
            continue

        block_name = fact.metadata.get("block_name")
        pointer_name = fact.metadata.get("pointer_name")
        block_start = fact.metadata.get("block_start")
        if not isinstance(block_name, str) or not isinstance(pointer_name, str):
            continue
        if not isinstance(block_start, int):
            continue

        grouped.setdefault((block_name, pointer_name), []).append(fact)

    return grouped


def _format_bad_values(values: tuple[int, ...]) -> str:
    """Compact sorted integers into concrete values and inclusive ranges."""
    spans: list[tuple[int, int]] = []
    start = previous = values[0]
    for value in values[1:]:
        if value == previous + 1:
            previous = value
            continue
        spans.append((start, previous))
        start = previous = value
    spans.append((start, previous))
    return ", ".join(str(start) if start == end else f"{start}..{end}" for start, end in spans)


def validate_pointer_defaults(
    program: Program,
    *,
    _context: ValidationContext | None = None,
) -> PointerDefaultReport:
    """Validate pointer defaults and complete domains at indirect dereferences."""
    from pyrung.core.validation.context import ValidationContext
    from pyrung.core.validation.pointer_sites import site_evidence

    grouped = _grouped_pointer_facts(program)
    if not grouped:
        return PointerDefaultReport(findings=())

    context = _context or ValidationContext(program)
    graph = context.graph
    findings: list[PointerDefaultFinding] = []

    for block_name, pointer_name in sorted(grouped):
        facts = grouped[(block_name, pointer_name)]
        first = facts[0]
        block_start = int(first.metadata["block_start"])
        block_end = int(first.metadata["block_end"])
        pointer_default_raw = first.metadata["pointer_default"]
        pointer_default = (
            int(pointer_default_raw)
            if isinstance(pointer_default_raw, bool)
            else int(pointer_default_raw)
        )
        target_name = f"{block_name}[{pointer_name}]"
        span = caret_of(target_name, pointer_name)
        pointer = graph.tags.get(pointer_name)
        if pointer is None:
            continue
        by_code: dict[str, list[OperandFact]] = {}
        invalid: dict[str, set[int]] = {}
        for fact in facts:
            domain, chains, default_possible = site_evidence(context, fact, pointer_name)

            def feasible(condition: Any, chains=chains) -> bool:
                return any(_conjunction_satisfiable((*chain, condition), {}) for chain in chains)

            bad = {
                int(value)
                for value in (domain or ())
                if isinstance(value, (bool, int))
                and not block_start <= int(value) <= block_end
                and feasible(CompareEq(pointer, int(value)))
            }
            if (
                default_possible
                and pointer_default < block_start
                and feasible(CompareEq(pointer, pointer_default))
            ):
                code = PTR_DEFAULT_BEFORE_BLOCK_START
                bad.add(pointer_default)
            elif bad:
                code = PTR_MAY_ESCAPE_BLOCK
            elif domain is None and (
                feasible(CompareLt(pointer, block_start)) or feasible(CompareGt(pointer, block_end))
            ):
                code = PTR_UNGUARDED_ACCESS
            else:
                continue
            by_code.setdefault(code, []).append(fact)
            invalid.setdefault(code, set()).update(bad)

        for code, sites in by_code.items():
            bad_values = tuple(sorted(invalid[code]))
            severity: Severity = "advisory" if code == PTR_UNGUARDED_ACCESS else "warning"
            if code == PTR_DEFAULT_BEFORE_BLOCK_START:
                label = f"defaults to {pointer_default}; valid {block_name} addresses are {block_start}..{block_end}"
                hint = (
                    f"set {pointer_name}'s default to {block_start}..{block_end}, or write it "
                    f"before using {target_name}; a range guard before the access also protects it"
                )
            elif code == PTR_MAY_ESCAPE_BLOCK:
                label = f"can use an invalid {block_name} address"
                hint = f"restrict {pointer_name} to {block_start}..{block_end}; possible invalid values: {_format_bad_values(bad_values)}"
            else:
                label = f"bounds not established for {block_name}[{pointer_name}]"
                hint = f"guard {pointer_name} with {block_start} <= {pointer_name} <= {block_end} before this access, or assign a known-valid index on every path reaching it"
            display = FindingDisplay(
                code=code,
                severity=severity,
                frames=tuple(_location_frame(f.location, target_name, span, label) for f in sites),
                hint=hint,
            )
            findings.append(
                PointerDefaultFinding(
                    code=code,
                    target_name=target_name,
                    block_name=block_name,
                    pointer_name=pointer_name,
                    pointer_default=pointer_default,
                    block_start=block_start,
                    block_end=block_end,
                    sites=tuple(f.location for f in sites),
                    display=display,
                    severity=severity,
                    bad_values=bad_values,
                )
            )

    return PointerDefaultReport(findings=tuple(findings))
