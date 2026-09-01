"""Indirect-pointer domain validation for pyrung programs.

Detects exact indirect dereference sites where the pointer tag's effective
default resolves below the indexed block's first valid address, or its complete
domain contains other concrete values outside the block.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from pyrung.core.condition import CompareEq
from pyrung.core.validation._common import (
    _conjunction_satisfiable,
    compact_location,
)
from pyrung.core.validation.display import FindingDisplay, Frame, _FindingTextMixin
from pyrung.core.validation.render import caret_of
from pyrung.core.validation.severity import Severity
from pyrung.core.validation.walker import OperandFact, ProgramLocation, walk_program

if TYPE_CHECKING:
    from pyrung.core.analysis.pdg import ProgramGraph
    from pyrung.core.analysis.return_guards import ReachChain
    from pyrung.core.program import Program
    from pyrung.core.validation.context import ValidationContext


PTR_DEFAULT_BEFORE_BLOCK_START = "PTR_DEFAULT_BEFORE_BLOCK_START"
PTR_MAY_ESCAPE_BLOCK = "PTR_MAY_ESCAPE_BLOCK"


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


def _pointer_written_before_read(graph: ProgramGraph, pointer_name: str) -> bool:
    """True when the PDG proves the pointer is unconditionally written before any read."""
    return graph.unconditional_write_before_read(pointer_name)


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


def _bad_values_at_site(
    pointer: Any,
    bad_values: tuple[int, ...],
    domains: dict[str, tuple[Any, ...]],
    chains: tuple[ReachChain, ...],
    *,
    narrow_guards: bool,
) -> tuple[int, ...]:
    """Bad values compatible with a stable pointer's effective site guards."""
    stable_conditions = tuple(chain.conditions for chain in chains if chain.return_stable)
    if not stable_conditions:
        return ()
    if not narrow_guards:
        return bad_values
    numeric_domains = {
        name: set(values)
        for name, values in domains.items()
        if all(isinstance(value, (int, float)) for value in values)
    }
    return tuple(
        value
        for value in bad_values
        if any(
            _conjunction_satisfiable((*chain, CompareEq(pointer, value)), numeric_domains)
            for chain in stable_conditions
        )
    )


def validate_pointer_defaults(
    program: Program,
    *,
    _context: ValidationContext | None = None,
) -> PointerDefaultReport:
    """Validate pointer defaults and complete domains at indirect dereferences."""
    from pyrung.core.analysis.return_guards import effective_reach_chains
    from pyrung.core.validation.context import ValidationContext

    grouped = _grouped_pointer_facts(program)
    if not grouped:
        return PointerDefaultReport(findings=())

    context = _context or ValidationContext(program)
    graph = context.graph
    domains = context.closed_domains
    reach = context.scope_reach_chains
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
        written_before_read = _pointer_written_before_read(graph, pointer_name)

        default_reported = pointer_default < block_start and not written_before_read
        if default_reported:
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

        # An unconditional write-before-read may sanitize values that persisted
        # from a later scan write. Without instruction-local domains, punt.
        domain = domains.get(pointer_name)
        if domain is None or written_before_read:
            continue
        candidate_bad_values = tuple(
            sorted(
                {
                    int(value)
                    for value in domain
                    if isinstance(value, (bool, int))
                    and not block_start <= int(value) <= block_end
                    and (not default_reported or int(value) != pointer_default)
                }
            )
        )
        pointer = graph.tags.get(pointer_name)
        unsafe_facts: list[OperandFact] = []
        bad_by_fact: list[tuple[int, ...]] = []
        for fact in facts:
            site_bad_values: tuple[int, ...] = ()
            node = graph.rung_node(
                scope=fact.location.scope,
                subroutine=fact.location.subroutine,
                rung_index=fact.location.rung_index,
                branch_path=fact.location.branch_path,
            )
            if pointer is not None and node is not None:
                site_bad_values = _bad_values_at_site(
                    pointer,
                    candidate_bad_values,
                    domains,
                    effective_reach_chains(
                        program,
                        graph,
                        node,
                        scope_chains=reach,
                    ),
                    narrow_guards=not graph.writers_of.get(pointer_name),
                )
            if site_bad_values:
                unsafe_facts.append(fact)
                bad_by_fact.append(site_bad_values)
        bad_values = tuple(sorted({value for values in bad_by_fact for value in values}))
        if not bad_values:
            continue
        values = _format_bad_values(bad_values)
        display = FindingDisplay(
            code=PTR_MAY_ESCAPE_BLOCK,
            severity="warning",
            frames=tuple(
                _location_frame(
                    fact.location,
                    target_name,
                    span,
                    f"can address outside {block_start}..{block_end}",
                )
                for fact in unsafe_facts
            ),
            hint=(
                f"restrict {pointer_name} to {block_start}..{block_end}; outside values: {values}"
            ),
        )
        findings.append(
            PointerDefaultFinding(
                code=PTR_MAY_ESCAPE_BLOCK,
                target_name=target_name,
                block_name=block_name,
                pointer_name=pointer_name,
                pointer_default=pointer_default,
                block_start=block_start,
                block_end=block_end,
                sites=tuple(f.location for f in unsafe_facts),
                display=display,
                severity="warning",
                bad_values=bad_values,
            )
        )

    return PointerDefaultReport(findings=tuple(findings))
