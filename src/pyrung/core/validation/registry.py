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

import warnings
from dataclasses import dataclass

from pyrung.core.validation.severity import Severity


@dataclass(frozen=True)
class RuleSpec:
    """Metadata for one validation rule code.

    ``validator`` is a key into ``report``'s validator dispatch, not a callable,
    so several codes can share one pass (STUCK_HIGH/LOW; the PHYS + RANGE family).
    ``aliases`` holds deprecated old codes accepted by ``select``/``ignore`` for
    one release (populated at the code rename).
    """

    code: str
    category: str  # "TAG" | "COIL" | "PTR" | "PHYS"  (+ "RUNG" | "CMP" when added)
    severity: Severity
    validator: str
    default_on: bool = True
    aliases: tuple[str, ...] = ()


_SPECS: tuple[RuleSpec, ...] = (
    RuleSpec("CORE_READONLY_WRITE", "TAG", "error", "readonly"),
    RuleSpec("CORE_CHOICES_VIOLATION", "TAG", "error", "choices"),
    RuleSpec("CORE_RANGE_VIOLATION", "TAG", "error", "physical"),
    RuleSpec("CORE_FINAL_MULTIPLE_WRITERS", "TAG", "warning", "final"),
    RuleSpec("CORE_CONFLICTING_OUTPUT", "COIL", "error", "conflicting"),
    RuleSpec("CORE_STUCK_HIGH", "COIL", "warning", "stuck"),
    RuleSpec("CORE_STUCK_LOW", "COIL", "warning", "stuck"),
    RuleSpec("CORE_POINTER_DEFAULT_BEFORE_BLOCK_START", "PTR", "warning", "pointer"),
    RuleSpec("CORE_MISSING_PROFILE", "PHYS", "info", "physical"),
    RuleSpec("CORE_ANTITOGGLE", "PHYS", "warning", "physical"),
)

RULES: dict[str, RuleSpec] = {spec.code: spec for spec in _SPECS}
ALL_RULES: frozenset[str] = frozenset(RULES)
CATEGORIES: frozenset[str] = frozenset(spec.category for spec in _SPECS)

# Deprecated alias -> current code.  Populated at the CORE_ rename (task #9).
ALIASES: dict[str, str] = {}

# Deterministic run order for the validator passes (stable finding output).
VALIDATOR_ORDER: tuple[str, ...] = (
    "stuck",
    "conflicting",
    "readonly",
    "pointer",
    "choices",
    "final",
    "physical",
)


def default_on_rules() -> frozenset[str]:
    """Codes active when ``select`` is not given (advisories opt out)."""
    return frozenset(code for code, spec in RULES.items() if spec.default_on)


def _expand(tokens: set[str]) -> set[str]:
    """Resolve each token to concrete codes: exact code, category, or alias."""
    out: set[str] = set()
    for tok in tokens:
        if tok in RULES:
            out.add(tok)
        elif tok in CATEGORIES:
            out |= {code for code, spec in RULES.items() if spec.category == tok}
        elif tok in ALIASES:
            new = ALIASES[tok]
            warnings.warn(
                f"Rule code {tok!r} is deprecated; use {new!r}.",
                DeprecationWarning,
                stacklevel=3,
            )
            out.add(new)
        else:
            raise ValueError(f"Unknown rule code or category: {tok!r}")
    return out


def resolve_rules(select: set[str] | None, ignore: set[str] | None) -> frozenset[str]:
    """Resolve ``select``/``ignore`` (codes or categories) to active codes.

    ``select=None`` means every default-on rule.  Unknown tokens raise
    ``ValueError``; deprecated aliases resolve with a ``DeprecationWarning``.
    """
    selected = _expand(select) if select is not None else set(default_on_rules())
    excluded = _expand(ignore) if ignore is not None else set()
    return frozenset(selected) - excluded
