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
    category: str
    severity: Severity
    validator: str
    title: str
    default_on: bool = False


_SPECS: tuple[RuleSpec, ...] = (
    RuleSpec("TAG_READONLY_WRITE", "TAG", "error", "readonly", "Write to Readonly Tag", True),
    RuleSpec(
        "TAG_CHOICES_VIOLATION",
        "TAG",
        "error",
        "choices",
        "Value Not Declared in Tag Choices",
        True,
    ),
    RuleSpec(
        "TAG_RANGE_VIOLATION",
        "TAG",
        "error",
        "physical",
        "Value Outside Tag's Declared Min/Max",
        True,
    ),
    RuleSpec(
        "TAG_DEAD_WRITE", "TAG", "warning", "dead_write", "Write Overwritten Before Read", True
    ),
    RuleSpec(
        "TAG_FINAL_MULTIPLE_WRITERS",
        "TAG",
        "error",
        "final",
        "Final Tag Has Multiple Writers",
        True,
    ),
    RuleSpec("COIL_CONFLICTING_OUTPUT", "COIL", "error", "conflicting", "Conflicting Output", True),
    RuleSpec("COIL_STUCK_HIGH", "COIL", "warning", "stuck", "Coil Can Stay High"),
    RuleSpec("COIL_STUCK_LOW", "COIL", "warning", "stuck", "Coil Can Stay Low"),
    RuleSpec(
        "PTR_DEFAULT_BEFORE_BLOCK_START",
        "PTR",
        "warning",
        "pointer",
        "Pointer Default Can Be Invalid",
        True,
    ),
    RuleSpec(
        "PTR_MAY_ESCAPE_BLOCK", "PTR", "warning", "pointer", "Pointer Value Can Be Invalid", True
    ),
    RuleSpec(
        "PTR_UNGUARDED_ACCESS", "PTR", "advisory", "pointer", "Pointer Bounds Not Established"
    ),
    RuleSpec("PHYS_MISSING_PROFILE", "PHYS", "info", "physical", "Missing Physical Profile"),
    RuleSpec(
        "PHYS_ANTITOGGLE",
        "PHYS",
        "warning",
        "physical",
        "Command Changes Too Fast for Feedback",
    ),
    RuleSpec(
        "RUNG_CONTRADICTION", "RUNG", "error", "rung", "Rung Never Fires (Contradiction)", True
    ),
    RuleSpec("RUNG_TAUTOLOGY", "RUNG", "warning", "rung", "Or() Condition Is Always True", True),
    RuleSpec("RUNG_REDUNDANT_TERM", "RUNG", "info", "rung", "Redundant Rung Condition"),
    RuleSpec("CMP_ALWAYS_FALSE", "CMP", "warning", "cmp", "Comparison Always False", True),
    RuleSpec("CMP_ALWAYS_TRUE", "CMP", "info", "cmp", "Comparison Always True"),
    RuleSpec(
        "CMP_EQ_ON_MONOTONE", "CMP", "warning", "cmp", "Timer/Counter Can Skip Compared Value", True
    ),
    RuleSpec(
        "CMP_OPERAND_NO_WRITER",
        "CMP",
        "advisory",
        "cmp",
        "Comparison Operand Has No Ladder Writer",
    ),
    RuleSpec(
        "CMP_PRESET_STAYS_ZERO",
        "CMP",
        "warning",
        "cmp",
        "Timer/Counter Preset Stays Zero",
    ),
    RuleSpec(
        "CMP_STEPPER_VALUE_NOT_SET",
        "CMP",
        "warning",
        "cmp",
        "Compared Tag Is Never Set to This Value",
    ),
    RuleSpec(
        "CMP_REPEATED_STATE_VALUE",
        "CMP",
        "advisory",
        "cmp",
        "Repeated Literal Comparisons",
    ),
    RuleSpec(
        "CMP_TRUE_AT_RESET",
        "CMP",
        "warning",
        "cmp",
        "Comparison Is True at Timer/Counter Reset Value",
    ),
    RuleSpec(
        "CMP_STATIC_ON_LEFT",
        "CMP",
        "advisory",
        "cmp",
        "Comparison May Read Backwards",
    ),
    RuleSpec("CALL_NEVER_CALLED", "CALL", "info", "call", "Subroutine Never Called"),
    RuleSpec("CALL_RECURSION", "CALL", "error", "call", "Recursive Subroutine Cycle", True),
    RuleSpec("MATH_DIV_ZERO", "MATH", "error", "math", "Definite Division by Zero", True),
    RuleSpec("STEP_NO_ESCAPE", "STEP", "warning", "wait", "Step Can Wait Forever"),
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
    "dead_write",
    "pointer",
    "choices",
    "final",
    "physical",
    "rung",
    "cmp",
    "call",
    "math",
    "wait",
)


def default_on_rules() -> frozenset[str]:
    """The curated core checks active when ``select`` is not given."""
    return frozenset(code for code, spec in RULES.items() if spec.default_on)


def _expand(tokens: set[str]) -> set[str]:
    """Resolve exact codes, prefixes, and ALL to concrete codes."""
    out: set[str] = set()
    for tok in tokens:
        matches = set(RULES) if tok == "ALL" else {code for code in RULES if code.startswith(tok)}
        if not tok or not matches:
            raise ValueError(f"Unknown rule code or category: {tok!r}")
        out.update(matches)
    return out


def resolve_rules(
    select: set[str] | None,
    ignore: set[str] | None,
    extend_select: set[str] | None = None,
) -> frozenset[str]:
    """Resolve selectors to active codes, with the most specific selector winning.

    ``select=None`` means every default-on rule.  Unknown tokens raise
    ``ValueError``. ``extend_select`` adds to the selection; ignore wins ties.
    """
    explicit = set(select or ()) | set(extend_select or ())
    selected = (set(select) if select is not None else set(default_on_rules())) | explicit
    ignored = set(ignore or ())
    candidates = _expand(selected)
    _expand(ignored)  # Validate even selectors that match no selected rules.

    def specificity(token: str) -> int:
        return 0 if token == "ALL" else len(token)

    def matches(token: str, code: str) -> bool:
        return token == "ALL" or code.startswith(token)

    return frozenset(
        code
        for code in candidates
        if max(specificity(t) if t in explicit else 0 for t in selected if matches(t, code))
        > max((specificity(t) for t in ignored if matches(t, code)), default=-1)
    )
