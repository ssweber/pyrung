"""Unified validation report and runner for all core validators."""

from __future__ import annotations

from collections.abc import Callable, Iterator
from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol, TypeVar

from pyrung.core.validation.registry import ALL_RULES, RULES, VALIDATOR_ORDER, resolve_rules
from pyrung.core.validation.severity import Severity

if TYPE_CHECKING:
    from pyrung.core.program import Program
    from pyrung.core.validation.display import FindingDisplay

__all__ = ["ALL_RULES", "Finding", "ValidationReport", "check", "validate"]


class Finding(Protocol):
    """Structural contract shared by all validation findings."""

    @property
    def code(self) -> str: ...

    @property
    def target_name(self) -> str: ...

    @property
    def message(self) -> str: ...

    @property
    def severity(self) -> Severity: ...

    @property
    def display(self) -> FindingDisplay:
        """Presentation structure; ``message`` is ``display.as_text()``."""
        ...


_FindingT = TypeVar("_FindingT", bound=Finding)


def _as_findings(findings: tuple[_FindingT, ...]) -> tuple[Finding, ...]:
    """Widen a concrete validator's finding tuple to the shared protocol."""
    return findings


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
        by_sev: dict[str, int] = {}
        for f in self.findings:
            by_sev[f.severity] = by_sev.get(f.severity, 0) + 1
        sev_parts = [
            f"{sev}: {by_sev[sev]}"
            for sev in ("error", "warning", "info", "advisory")
            if sev in by_sev
        ]
        return f"{len(self.findings)} finding(s) [{', '.join(sev_parts)}] ({', '.join(parts)})"

    def errors(self) -> tuple[Finding, ...]:
        """Findings at ``error`` severity — the CI gate."""
        return tuple(f for f in self.findings if f.severity == "error")

    def warnings(self) -> tuple[Finding, ...]:
        return tuple(f for f in self.findings if f.severity == "warning")

    def infos(self) -> tuple[Finding, ...]:
        return tuple(f for f in self.findings if f.severity == "info")

    def advisories(self) -> tuple[Finding, ...]:
        return tuple(f for f in self.findings if f.severity == "advisory")

    def has_errors(self) -> bool:
        return any(f.severity == "error" for f in self.findings)

    def __bool__(self) -> bool:
        return bool(self.findings)

    def __len__(self) -> int:
        return len(self.findings)

    def __iter__(self) -> Iterator[Finding]:
        return iter(self.findings)


def check(
    program: Program,
    *,
    select: set[str] | None = None,
    ignore: set[str] | None = None,
    dt: float = 0.010,
) -> ValidationReport:
    """Run core ladder checks, optionally filtered by rule code or category.

    With no arguments, every default-on validator runs.  ``select`` limits to
    the given codes or category prefixes (e.g. ``{"COIL"}``); ``ignore``
    excludes them.  Both may be combined (``select - ignore``).  Unknown tokens
    raise ``ValueError``.

    ``dt`` is forwarded to the physical-realism validator.
    """
    active = resolve_rules(select, ignore)
    if not active:
        return ValidationReport(findings=())

    needed = {RULES[code].validator for code in active}
    dispatch = _validator_dispatch(program, dt)

    findings: list[Finding] = []
    for key in VALIDATOR_ORDER:
        if key not in needed:
            continue
        for f in dispatch[key]():
            if f.code in active:
                findings.append(f)
    return ValidationReport(findings=tuple(findings))


def validate(
    program: Program,
    *,
    select: set[str] | None = None,
    ignore: set[str] | None = None,
    dt: float = 0.010,
) -> ValidationReport:
    """Compatibility alias for :func:`check`."""
    return check(program, select=select, ignore=ignore, dt=dt)


def _validator_dispatch(
    program: Program, dt: float
) -> dict[str, Callable[[], tuple[Finding, ...]]]:
    """Map each validator key to a thunk returning that pass's findings.

    Imports are local so importing this module never eagerly loads the validator
    modules — matching the historical import profile of :func:`validate`.
    """
    from pyrung.core.validation.choices_violation import validate_choices
    from pyrung.core.validation.cmp_conditions import validate_cmp_conditions
    from pyrung.core.validation.duplicate_out import validate_conflicting_outputs
    from pyrung.core.validation.final_writers import validate_final_writers
    from pyrung.core.validation.physical_realism import validate_physical_realism
    from pyrung.core.validation.pointer_default import validate_pointer_defaults
    from pyrung.core.validation.readonly_write import validate_readonly_writes
    from pyrung.core.validation.rung_conditions import validate_rung_conditions
    from pyrung.core.validation.stuck_bits import validate_stuck_bits
    from pyrung.core.validation.wait_escape import validate_wait_escapes

    return {
        "stuck": lambda: _as_findings(validate_stuck_bits(program).findings),
        "conflicting": lambda: _as_findings(validate_conflicting_outputs(program).findings),
        "readonly": lambda: _as_findings(validate_readonly_writes(program).findings),
        "pointer": lambda: _as_findings(validate_pointer_defaults(program).findings),
        "choices": lambda: _as_findings(validate_choices(program).findings),
        "final": lambda: _as_findings(validate_final_writers(program).findings),
        "physical": lambda: _as_findings(validate_physical_realism(program, dt=dt).findings),
        "rung": lambda: _as_findings(validate_rung_conditions(program).findings),
        "cmp": lambda: _as_findings(validate_cmp_conditions(program).findings),
        "wait": lambda: _as_findings(validate_wait_escapes(program).findings),
    }
