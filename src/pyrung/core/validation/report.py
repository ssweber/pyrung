"""Unified validation report and runner for all core validators."""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from pyrung.core.program import Program


class Finding(Protocol):
    """Structural contract shared by all validation findings."""

    code: str
    target_name: str
    message: str


ALL_RULES: frozenset[str] = frozenset(
    {
        "CORE_ANTITOGGLE",
        "CORE_CHOICES_VIOLATION",
        "CORE_CONFLICTING_OUTPUT",
        "CORE_FINAL_MULTIPLE_WRITERS",
        "CORE_MISSING_PROFILE",
        "CORE_RANGE_VIOLATION",
        "CORE_READONLY_WRITE",
        "CORE_STUCK_HIGH",
        "CORE_STUCK_LOW",
    }
)


@dataclass(frozen=True)
class ValidationReport:
    """Unified report from all core validators."""

    findings: tuple[Finding, ...]

    def summary(self) -> str:
        if not self.findings:
            return "No findings."
        by_code: dict[str, int] = {}
        for f in self.findings:
            by_code[f.code] = by_code.get(f.code, 0) + 1
        parts = [f"{code}: {n}" for code, n in sorted(by_code.items())]
        return f"{len(self.findings)} finding(s) ({', '.join(parts)})"

    def __bool__(self) -> bool:
        return bool(self.findings)

    def __len__(self) -> int:
        return len(self.findings)

    def __iter__(self) -> Iterator[Finding]:
        return iter(self.findings)


def _resolve_rules(
    select: set[str] | None,
    ignore: set[str] | None,
) -> frozenset[str]:
    unknown = ((select or set()) | (ignore or set())) - ALL_RULES
    if unknown:
        raise ValueError(f"Unknown rule code(s): {', '.join(sorted(unknown))}")
    active = frozenset(select) if select is not None else ALL_RULES
    if ignore is not None:
        active = active - ignore
    return active


def validate(
    program: Program,
    *,
    select: set[str] | None = None,
    ignore: set[str] | None = None,
    dt: float = 0.010,
) -> ValidationReport:
    """Run core validators, optionally filtered by rule code.

    With no arguments, all validators run.  ``select`` limits to the given
    codes; ``ignore`` excludes codes.  Both may be combined (``select -
    ignore``).  Unknown codes raise ``ValueError``.

    ``dt`` is forwarded to the physical-realism validator.
    """
    active = _resolve_rules(select, ignore)
    if not active:
        return ValidationReport(findings=())

    from pyrung.core.validation.choices_violation import validate_choices
    from pyrung.core.validation.duplicate_out import validate_conflicting_outputs
    from pyrung.core.validation.final_writers import validate_final_writers
    from pyrung.core.validation.physical_realism import validate_physical_realism
    from pyrung.core.validation.readonly_write import validate_readonly_writes
    from pyrung.core.validation.stuck_bits import validate_stuck_bits

    findings: list[Finding] = []

    if active & {"CORE_STUCK_HIGH", "CORE_STUCK_LOW"}:
        for f in validate_stuck_bits(program).findings:
            if f.code in active:
                findings.append(f)

    if "CORE_CONFLICTING_OUTPUT" in active:
        findings.extend(validate_conflicting_outputs(program).findings)

    if "CORE_READONLY_WRITE" in active:
        findings.extend(validate_readonly_writes(program).findings)

    if "CORE_CHOICES_VIOLATION" in active:
        findings.extend(validate_choices(program).findings)

    if "CORE_FINAL_MULTIPLE_WRITERS" in active:
        findings.extend(validate_final_writers(program).findings)

    if active & {"CORE_RANGE_VIOLATION", "CORE_MISSING_PROFILE", "CORE_ANTITOGGLE"}:
        for f in validate_physical_realism(program, dt=dt).findings:
            if f.code in active:
                findings.append(f)

    return ValidationReport(findings=tuple(findings))
