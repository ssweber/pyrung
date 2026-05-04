"""Pointer default validation for pyrung programs.

Detects exact indirect dereference sites where the pointer tag's effective
default resolves below the indexed block's first valid address.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from pyrung.core.validation.walker import OperandFact, ProgramLocation, walk_program

if TYPE_CHECKING:
    from pyrung.core.program import Program


CORE_POINTER_DEFAULT_BEFORE_BLOCK_START = "CORE_POINTER_DEFAULT_BEFORE_BLOCK_START"


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
    message: str


@dataclass(frozen=True)
class PointerDefaultReport:
    findings: tuple[PointerDefaultFinding, ...]

    def summary(self) -> str:
        if not self.findings:
            return "No pointer default violations."
        return f"{len(self.findings)} pointer default violation(s)."


def _format_location(site: ProgramLocation) -> str:
    """Render a compact, deterministic site label for a walker fact."""
    if site.scope == "main":
        prefix = f"main rung {site.rung_index}"
    else:
        prefix = f"subroutine {site.subroutine!r} rung {site.rung_index}"

    parts = [prefix]
    if site.branch_path:
        branch = ".".join(str(part) for part in site.branch_path)
        parts.append(f"branch {branch}")
    if site.instruction_index is not None:
        if site.instruction_type is not None:
            parts.append(f"instruction {site.instruction_index} ({site.instruction_type})")
        else:
            parts.append(f"instruction {site.instruction_index}")
    parts.append(site.arg_path)
    return ", ".join(parts)


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


def validate_pointer_defaults(program: Program) -> PointerDefaultReport:
    """Validate a Program for indirect pointers defaulting below block start."""
    grouped = _grouped_pointer_facts(program)
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
        site_lines = "\n".join(f"  - {_format_location(f.location)}" for f in facts)
        message = (
            f"Pointer dereference '{target_name}' uses effective default {pointer_default}, "
            f"below {block_name} block start {block_start} "
            f"(valid: {block_start}-{block_end}). "
            "Write the address into a separately initialized pointer tag before "
            "dereferencing this block.\n"
            f"Sites:\n{site_lines}"
        )
        findings.append(
            PointerDefaultFinding(
                code=CORE_POINTER_DEFAULT_BEFORE_BLOCK_START,
                target_name=target_name,
                block_name=block_name,
                pointer_name=pointer_name,
                pointer_default=pointer_default,
                block_start=block_start,
                block_end=block_end,
                sites=tuple(f.location for f in facts),
                message=message,
            )
        )

    return PointerDefaultReport(findings=tuple(findings))
