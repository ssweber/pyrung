"""Central rule registry: the single source of truth for validator metadata.

Each rule has a :class:`RuleSpec` carrying its category, default severity, the
validator that emits it, and (post-rename) any deprecated code aliases.  The
registry replaces the hand-maintained if-ladder in :func:`report.validate`:
``validate`` resolves the requested codes/categories against :data:`RULES`,
runs only the validators that can emit an active code, and filters.

This module is deliberately import-light — it pulls in nothing but the
``severity`` vocabulary — so importing it never eagerly loads the validator
modules (that stays lazy inside :func:`report.validate`).
"""

from __future__ import annotations

from dataclasses import dataclass

from pyrung.core.validation.severity import SEVERITY_ORDER, Severity


@dataclass(frozen=True)
class RuleSpec:
    """Fully self-describing metadata for one validation rule code.

    Everything a UI needs to display a rule lives here — code, category,
    severity, and a human ``title`` — so consumers (e.g. clicknick's Analyze
    Program window) render from the registry and never hard-code their own copy.

    ``validator`` is a key into ``report``'s validator dispatch, not a callable,
    so several codes can share one pass (STUCK_HIGH/LOW; the PHYS + RANGE family).
    """

    code: str
    category: str  # "TAG" | "COIL" | "PTR" | "PHYS" | "RUNG"  (+ "CMP" when added)
    severity: Severity
    validator: str
    title: str
    default_on: bool = True


_SPECS: tuple[RuleSpec, ...] = (
    RuleSpec("TAG_READONLY_WRITE", "TAG", "error", "readonly", "Writes to Read-Only"),
    RuleSpec("TAG_CHOICES_VIOLATION", "TAG", "error", "choices", "Choices Violation"),
    RuleSpec("TAG_RANGE_VIOLATION", "TAG", "error", "physical", "Range Violation"),
    RuleSpec(
        "TAG_FINAL_MULTIPLE_WRITERS", "TAG", "warning", "final", "Final Tag — Multiple Writers"
    ),
    RuleSpec("COIL_CONFLICTING_OUTPUT", "COIL", "error", "conflicting", "Conflicting Output"),
    RuleSpec("COIL_STUCK_HIGH", "COIL", "warning", "stuck", "Stuck High (never reset)"),
    RuleSpec("COIL_STUCK_LOW", "COIL", "warning", "stuck", "Stuck Low (never latched)"),
    RuleSpec(
        "PTR_DEFAULT_BEFORE_BLOCK_START",
        "PTR",
        "warning",
        "pointer",
        "Pointer Default Before Block",
    ),
    RuleSpec("PHYS_MISSING_PROFILE", "PHYS", "info", "physical", "Missing Physical Profile"),
    RuleSpec("PHYS_ANTITOGGLE", "PHYS", "warning", "physical", "Anti-Toggle Oscillation"),
    RuleSpec("RUNG_CONTRADICTION", "RUNG", "error", "rung", "Rung Never Fires (Contradiction)"),
    RuleSpec("RUNG_TAUTOLOGY", "RUNG", "warning", "rung", "Always-True Or Term (Tautology)"),
    RuleSpec("CMP_EQ_ON_MONOTONE", "CMP", "error", "cmp", "Equality vs Self-Advancing Register"),
    RuleSpec("CMP_TRUE_AT_RESET", "CMP", "warning", "cmp", "Comparison True at Reset Value"),
    RuleSpec("CMP_STATIC_ON_LEFT", "CMP", "advisory", "cmp", "Static Operand on Left"),
)

RULES: dict[str, RuleSpec] = {spec.code: spec for spec in _SPECS}
ALL_RULES: frozenset[str] = frozenset(RULES)
CATEGORIES: frozenset[str] = frozenset(spec.category for spec in _SPECS)


def ordered_rules() -> tuple[RuleSpec, ...]:
    """All rule specs in canonical display order: severity desc, then category, code.

    The single source a UI iterates to lay out a report — most severe first.
    """
    return tuple(sorted(_SPECS, key=lambda s: (-SEVERITY_ORDER[s.severity], s.category, s.code)))


# Deterministic run order for the validator passes (stable finding output).
VALIDATOR_ORDER: tuple[str, ...] = (
    "stuck",
    "conflicting",
    "readonly",
    "pointer",
    "choices",
    "final",
    "physical",
    "rung",
    "cmp",
)


def default_on_rules() -> frozenset[str]:
    """Codes active when ``select`` is not given (advisories opt out)."""
    return frozenset(code for code, spec in RULES.items() if spec.default_on)


def _expand(tokens: set[str]) -> set[str]:
    """Resolve each token to concrete codes: an exact code or a category."""
    out: set[str] = set()
    for tok in tokens:
        if tok in RULES:
            out.add(tok)
        elif tok in CATEGORIES:
            out |= {code for code, spec in RULES.items() if spec.category == tok}
        else:
            raise ValueError(f"Unknown rule code or category: {tok!r}")
    return out


def resolve_rules(select: set[str] | None, ignore: set[str] | None) -> frozenset[str]:
    """Resolve ``select``/``ignore`` (codes or categories) to active codes.

    ``select=None`` means every default-on rule.  Unknown tokens raise
    ``ValueError``.
    """
    selected = _expand(select) if select is not None else set(default_on_rules())
    excluded = _expand(ignore) if ignore is not None else set()
    return frozenset(selected) - excluded
