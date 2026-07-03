"""Rung-condition satisfiability validators: RUNG_CONTRADICTION / RUNG_TAUTOLOGY.

Two rules, one pass over ``program.rungs`` (main + subroutines + branches):

* ``RUNG_CONTRADICTION`` — the rung's condition conjunction is provably
  unsatisfiable, so the rung can never fire.  ``not conjunction_satisfiable(...)``
  over the existing interval/domain solver; **error** severity (UNSAT anywhere is
  provably wrong, no input sequence fixes it).  Bare ``rung()`` — the intentional
  always-on rung — is skipped.

* ``RUNG_TAUTOLOGY`` — a top-level ``Or(...)`` conjunct is provably always-true
  (canonically ``Or(x != a, x != b, x != c)`` over one variable), so it gates
  nothing; the rung's real condition is the residual.  **warning** severity, and
  the message shows the residual explicitly — half the diagnostic value is making
  the real gate visible before saying "this never fires."

Ladder has no group-negation primitive (series is AND, parallel is OR), so
"reject when NOT valid" forces the engineer to distribute a negation by hand
across the diagram — the exact operation these two rules catch when it goes
wrong.  Both build entirely on :mod:`pyrung.core.validation.sat`; no new solver.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from pyrung.core.condition import (
    AllCondition,
    AnyCondition,
    BitCondition,
    CompareEq,
    CompareGe,
    CompareGt,
    CompareLe,
    CompareLt,
    CompareNe,
    FallingEdgeCondition,
    IntTruthyCondition,
    NormallyClosedCondition,
    RisingEdgeCondition,
)
from pyrung.core.tag import ImmediateRef, Tag
from pyrung.core.validation._common import _flatten_and_conditions
from pyrung.core.validation.sat import (
    conjunction_satisfiable,
    disjunction_tautological,
)
from pyrung.core.validation.severity import Severity

if TYPE_CHECKING:
    from collections.abc import Iterator, Sequence

    from pyrung.core.condition import Condition
    from pyrung.core.program import Program
    from pyrung.core.rung import Rung

RUNG_CONTRADICTION = "RUNG_CONTRADICTION"
RUNG_TAUTOLOGY = "RUNG_TAUTOLOGY"

# ---------------------------------------------------------------------------
# Condition rendering (Condition-level; simplified.render works on Expr/Atom)
# ---------------------------------------------------------------------------

_COMPARE_SYMBOLS: dict[type, str] = {
    CompareEq: "==",
    CompareNe: "!=",
    CompareLt: "<",
    CompareLe: "<=",
    CompareGt: ">",
    CompareGe: ">=",
}


def _operand_name(value: object) -> str:
    """Human name for a comparison operand — tag name, or the literal itself."""
    if isinstance(value, ImmediateRef):
        return _operand_name(value.value)
    if isinstance(value, Tag):
        return value.name
    return str(value)


def _render_condition(cond: Condition) -> str:
    """Render a leaf/compound Condition as a human-readable string.

    A small Condition-level renderer; ``simplified.render`` operates on the
    ``Expr``/``Atom`` form, not on live ``Condition`` objects.
    """
    if isinstance(cond, (CompareEq, CompareNe, CompareLt, CompareLe, CompareGt, CompareGe)):
        sym = _COMPARE_SYMBOLS[type(cond)]
        return f"{_operand_name(cond.tag)} {sym} {_operand_name(cond.value)}"
    if isinstance(cond, BitCondition):
        return _operand_name(cond.tag)
    if isinstance(cond, NormallyClosedCondition):
        return f"~{_operand_name(cond.tag)}"
    if isinstance(cond, IntTruthyCondition):
        return f"{_operand_name(cond.tag)} != 0"
    if isinstance(cond, RisingEdgeCondition):
        return f"rise({_operand_name(cond.tag)})"
    if isinstance(cond, FallingEdgeCondition):
        return f"fall({_operand_name(cond.tag)})"
    if isinstance(cond, AnyCondition):
        return f"Or({', '.join(_render_condition(c) for c in cond.conditions)})"
    if isinstance(cond, AllCondition):
        return f"And({', '.join(_render_condition(c) for c in cond.conditions)})"
    return type(cond).__name__


def _render_conjunction(conds: Sequence[Condition]) -> str:
    """Render an AND-chain of rung conditions as ``a AND b AND c``."""
    return " AND ".join(_render_condition(c) for c in conds)


# ---------------------------------------------------------------------------
# Rung walking
# ---------------------------------------------------------------------------


def _iter_rungs(program: Program) -> Iterator[tuple[str, Rung]]:
    """Yield ``(location, rung)`` for every rung: main, subroutines, branches.

    Each branch is its own rung with its own ``_conditions`` — a branch whose own
    conditions are contradictory is genuinely dead, so it is worth walking.
    """

    def _walk(rung: Rung, prefix: str) -> Iterator[tuple[str, Rung]]:
        yield prefix, rung
        for branch_idx, branch in enumerate(rung._branches):
            yield from _walk(branch, f"{prefix} branch {branch_idx}")

    for rung_index, rung in enumerate(program.rungs):
        yield from _walk(rung, f"rung {rung_index + 1}")
    for sub_name in sorted(program.subroutines):
        for rung_index, rung in enumerate(program.subroutines[sub_name]):
            yield from _walk(rung, f"subroutine '{sub_name}' rung {rung_index + 1}")


# ---------------------------------------------------------------------------
# Blocking-pair + De Morgan repair hint (spec §4.3)
# ---------------------------------------------------------------------------


def _blocking_pair(conds: Sequence[Condition]) -> tuple[Condition, Condition] | None:
    """First pair of leaf conditions whose conjunction is UNSAT, for the message.

    ``AllCondition`` wrappers are flattened to leaves; opaque ``AnyCondition``
    terms are kept but never form a provable pair (they read as satisfiable).
    """
    leaves = _flatten_and_conditions(tuple(conds))
    for i in range(len(leaves)):
        for j in range(i + 1, len(leaves)):
            if not conjunction_satisfiable([leaves[i], leaves[j]]):
                return leaves[i], leaves[j]
    return None


def _flatten_or_terms(conds: Sequence[Condition]) -> list[Condition]:
    """Expand top-level ``Or`` conjuncts into their disjuncts.

    The De Morgan dual of a rung's implicit AND is the OR of the same terms; when
    a term is itself an ``Or`` (e.g. the tautological state gate), it must be
    flattened before testing the dual, or the always-true term hides as opaque
    and the dual reads as informative when it is really a tautology.
    """
    out: list[Condition] = []
    for cond in conds:
        if isinstance(cond, AnyCondition):
            out.extend(cond.conditions)
        else:
            out.append(cond)
    return out


def _disjunction_satisfiable(terms: Sequence[Condition]) -> bool:
    """True when ``Or(*terms)`` can be true — i.e. some term is satisfiable."""
    return any(conjunction_satisfiable([t]) for t in terms)


def _demorgan_hint(conds: Sequence[Condition]) -> str | None:
    """A ``did you mean Or(...)`` hint, or None when the flip is not informative.

    The dual is emitted only when it is genuinely a repair: satisfiable (it can
    fire) and not tautological (it says something).  On the double-De-Morgan slip
    — where the naive OR is itself always-true — this correctly returns None; the
    honest single-level case (``x < 1 AND x > 3`` → ``Or(x < 1, x > 3)``) fires.
    """
    terms = _flatten_or_terms(conds)
    if disjunction_tautological(terms):
        return None  # naive flip is always-true — not a real repair
    if not _disjunction_satisfiable(terms):
        return None  # dual never fires either
    return f"Or({', '.join(_render_condition(t) for t in terms)})"


# ---------------------------------------------------------------------------
# Messages
# ---------------------------------------------------------------------------


def _contradiction_message(conds: Sequence[Condition]) -> str:
    lines = [
        "Rung condition simplifies to False — this rung can never fire.",
        f"  condition:  {_render_conjunction(conds)}",
    ]
    pair = _blocking_pair(conds)
    if pair is not None:
        a, b = pair
        lines.append(
            f"  blocking:   {_render_condition(a)} and {_render_condition(b)} cannot both hold"
        )
    hint = _demorgan_hint(conds)
    if hint is not None:
        lines.append(f"  did you mean:  {hint}")
    return "\n".join(lines)


def _tautology_message(taut: Sequence[Condition], residual: Sequence[Condition]) -> str:
    always_true = "; ".join(_render_condition(c) for c in taut)
    if residual:
        reduces = f"  reduces to:   {_render_conjunction(residual)}"
    else:
        reduces = "  reduces to:   always true — this rung fires every scan"
    return "\n".join(
        [
            "Rung contains an always-true Or term that gates nothing.",
            f"  always-true:  {always_true}",
            reduces,
        ]
    )


# ---------------------------------------------------------------------------
# Public types
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class RungConditionFinding:
    """A rung-level satisfiability finding (contradiction or tautology)."""

    code: str
    target_name: str
    message: str
    severity: Severity


@dataclass(frozen=True)
class RungConditionReport:
    findings: tuple[RungConditionFinding, ...]

    def summary(self) -> str:
        if not self.findings:
            return "No rung-condition findings."
        return f"{len(self.findings)} rung-condition finding(s)."


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def validate_rung_conditions(program: Program) -> RungConditionReport:
    """Validate rung conditions for contradictions and tautological Or terms.

    One pass emitting two codes:

    * ``RUNG_CONTRADICTION`` when a rung's condition conjunction is provably
      unsatisfiable (skips the intentional bare ``rung()``).
    * ``RUNG_TAUTOLOGY`` when a top-level ``Or`` conjunct is provably always-true.

    Both grades of a single rung are reported: the buggy guard rung is both a
    contradiction and carries a tautological Or term.
    """
    findings: list[RungConditionFinding] = []

    for loc, rung in _iter_rungs(program):
        conds = tuple(rung._conditions)
        if not conds:
            continue  # bare rung() — the intentional always-on rung

        if not conjunction_satisfiable(conds):
            findings.append(
                RungConditionFinding(
                    code=RUNG_CONTRADICTION,
                    target_name=loc,
                    message=_contradiction_message(conds),
                    severity="error",
                )
            )

        taut = [
            c
            for c in conds
            if isinstance(c, AnyCondition) and disjunction_tautological(c.conditions)
        ]
        if taut:
            residual = tuple(c for c in conds if c not in taut)
            findings.append(
                RungConditionFinding(
                    code=RUNG_TAUTOLOGY,
                    target_name=loc,
                    message=_tautology_message(taut, residual),
                    severity="warning",
                )
            )

    return RungConditionReport(findings=tuple(findings))
