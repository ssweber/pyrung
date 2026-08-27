"""Pointer default validation for pyrung programs.

Detects exact indirect dereference sites where the pointer tag's effective
default resolves below the indexed block's first valid address.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from pyrung.core.validation._common import compact_location
from pyrung.core.validation.display import FindingDisplay, Frame
from pyrung.core.validation.render import caret_of
from pyrung.core.validation.severity import Severity
from pyrung.core.validation.walker import OperandFact, ProgramLocation, walk_program

if TYPE_CHECKING:
    from pyrung.core.analysis.pdg import ProgramGraph
    from pyrung.core.program import Program


PTR_DEFAULT_BEFORE_BLOCK_START = "PTR_DEFAULT_BEFORE_BLOCK_START"


@dataclass(frozen=True)
class PointerDefaultFinding:
    """An indirect block dereference whose pointer defaults below block start."""

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
    """Collect indirect dereference facts whose pointer default is below block start."""
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

        pointer_default_raw = fact.metadata.get("pointer_default")
        if isinstance(pointer_default_raw, bool):
            pointer_default = int(pointer_default_raw)
        elif isinstance(pointer_default_raw, int):
            pointer_default = pointer_default_raw
        else:
            continue

        if pointer_default >= block_start:
            continue

        grouped.setdefault((block_name, pointer_name), []).append(fact)

    return grouped


def _pointer_written_before_read(graph: ProgramGraph, pointer_name: str) -> bool:
    """True when the PDG proves the pointer is unconditionally written before any read."""
    return graph.unconditional_write_before_read(pointer_name)


def validate_pointer_defaults(program: Program) -> PointerDefaultReport:
    """Validate a Program for indirect pointers defaulting below block start."""
    from pyrung.core.analysis.pdg import build_program_graph

    grouped = _grouped_pointer_facts(program)
    if not grouped:
        return PointerDefaultReport(findings=())

    graph = build_program_graph(program)
    findings: list[PointerDefaultFinding] = []

    for block_name, pointer_name in sorted(grouped):
        if _pointer_written_before_read(graph, pointer_name):
            continue
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
        label = f"can be {pointer_default}, before {block_name}[{block_start}]"
        display = FindingDisplay(
            code=PTR_DEFAULT_BEFORE_BLOCK_START,
            severity="warning",
            frames=tuple(_location_frame(f.location, target_name, span, label) for f in facts),
            hint=(
                f"set {pointer_name} to {block_start}..{block_end} before using "
                f"{block_name}[{pointer_name}]"
            ),
        )
        findings.append(
            PointerDefaultFinding(
                code=PTR_DEFAULT_BEFORE_BLOCK_START,
                target_name=target_name,
                block_name=block_name,
                pointer_name=pointer_name,
                pointer_default=pointer_default,
                block_start=block_start,
                block_end=block_end,
                sites=tuple(f.location for f in facts),
                display=display,
            )
        )

    return PointerDefaultReport(findings=tuple(findings))
