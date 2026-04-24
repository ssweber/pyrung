"""Choices constraint validation for pyrung programs.

Detects write sites where a literal value is written to a tag whose
``choices`` key set does not include that value.  Only statically-resolvable
literal writes are checked — dynamic/expression writes are skipped.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from pyrung.core.tag import Tag
from pyrung.core.validation._common import (
    WriteSite,
    _build_tag_map,
    _format_site_location,
    _resolve_tag_names,
)

if TYPE_CHECKING:
    from pyrung.core.program import Program

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

CORE_CHOICES_VIOLATION = "CORE_CHOICES_VIOLATION"

# ---------------------------------------------------------------------------
# Public types
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ChoicesViolationFinding:
    """A write site that writes a literal value outside the tag's choices."""

    code: str
    target_name: str
    value: Any
    allowed: tuple[Any, ...]
    site: WriteSite
    message: str


@dataclass(frozen=True)
class ChoicesViolationReport:
    findings: tuple[ChoicesViolationFinding, ...]

    def summary(self) -> str:
        if not self.findings:
            return "No choices violations."
        return f"{len(self.findings)} choices violation(s)."


# ---------------------------------------------------------------------------
# Literal value extraction
# ---------------------------------------------------------------------------


def _literal_copy_targets(instr: Any) -> list[tuple[str, str, Any]]:
    """Return (tag_name, instruction_type, literal_value) for copy instructions
    where the source is a literal (not a tag or expression)."""
    from pyrung.core.instruction.data_transfer import CopyInstruction, FillInstruction

    itype = type(instr).__name__

    if isinstance(instr, CopyInstruction):
        source = instr.source
        if isinstance(source, (Tag, type(None))) or hasattr(source, "_pyrung_structure_runtime"):
            return []  # dynamic source
        if isinstance(source, (int, float, str)):
            names = _resolve_tag_names(instr.target)
            return [(name, itype, source) for name in names]

    if isinstance(instr, FillInstruction):
        source = instr.source
        if isinstance(source, (int, float, str)):
            names = _resolve_tag_names(instr.target)
            return [(name, itype, source) for name in names]

    return []


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def validate_choices(program: Program) -> ChoicesViolationReport:
    """Validate a Program for literal writes that violate choices constraints."""
    tag_map = _build_tag_map(program)

    # Custom walk: collect write sites with literal values
    from pyrung.core.instruction.control import ForLoopInstruction
    from pyrung.core.validation._common import FactScope

    literal_sites: list[tuple[WriteSite, Any]] = []

    def _walk_instructions(
        instructions: list[Any],
        scope: FactScope,
        subroutine: str | None,
        rung_index: int,
        branch_path: tuple[int, ...],
        conditions: tuple[Any, ...],
    ) -> None:
        for instr_idx, instr in enumerate(instructions):
            for tag_name, itype, value in _literal_copy_targets(instr):
                site = WriteSite(
                    target_name=tag_name,
                    scope=scope,
                    subroutine=subroutine,
                    rung_index=rung_index,
                    branch_path=branch_path,
                    instruction_index=instr_idx,
                    instruction_type=itype,
                    conditions=conditions,
                    source_file=getattr(instr, "source_file", None),
                    source_line=getattr(instr, "source_line", None),
                )
                literal_sites.append((site, value))
            if isinstance(instr, ForLoopInstruction) and hasattr(instr, "instructions"):
                _walk_instructions(
                    instr.instructions, scope, subroutine, rung_index, branch_path, conditions
                )

    def _walk_rung(
        rung: Any,
        scope: FactScope,
        subroutine: str | None,
        rung_index: int,
        branch_path: tuple[int, ...],
    ) -> None:
        conditions = tuple(rung._conditions)
        _walk_instructions(
            rung._instructions, scope, subroutine, rung_index, branch_path, conditions
        )
        for branch_idx, branch_rung in enumerate(rung._branches):
            _walk_rung(branch_rung, scope, subroutine, rung_index, branch_path + (branch_idx,))

    for rung_index, rung in enumerate(program.rungs):
        _walk_rung(rung, "main", None, rung_index, ())
    for sub_name in sorted(program.subroutines):
        for rung_index, rung in enumerate(program.subroutines[sub_name]):
            _walk_rung(rung, "subroutine", sub_name, rung_index, ())

    # Check each literal write against the tag's choices
    findings: list[ChoicesViolationFinding] = []
    for site, value in literal_sites:
        tag = tag_map.get(site.target_name)
        if tag is None or tag.choices is None:
            continue
        if value not in tag.choices:
            allowed = tuple(tag.choices.keys())
            loc = _format_site_location(site)
            message = (
                f"Tag '{site.target_name}' has choices {allowed} "
                f"but write site copies literal {value!r}:\n  - {loc}"
            )
            findings.append(
                ChoicesViolationFinding(
                    code=CORE_CHOICES_VIOLATION,
                    target_name=site.target_name,
                    value=value,
                    allowed=allowed,
                    site=site,
                    message=message,
                )
            )

    return ChoicesViolationReport(findings=tuple(findings))
