"""Rung-condition satisfiability and redundancy validators.

Three rules, one pass over ``program.rungs`` (main + subroutines + branches):

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

* ``RUNG_REDUNDANT_TERM`` — an exact duplicate or a term subsumed by another
  term in the same And/Or connective. **info** severity; stronger contradiction
  and tautology findings own their groups.

Ladder has no group-negation primitive (series is AND, parallel is OR), so
"reject when NOT valid" forces the engineer to distribute a negation by hand
across the diagram — the exact operation these two rules catch when it goes
wrong.  Both build entirely on :mod:`pyrung.core.validation.sat`; no new solver.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from pyrung.core.condition import (
    AllCondition,
    AnyCondition,
    CompareEq,
    CompareGe,
    CompareGt,
    CompareLe,
    CompareLt,
    CompareNe,
)
from pyrung.core.tag import ImmediateRef, Tag
from pyrung.core.validation._common import (
    DomainMap,
    RungLoc,
    _conjunction_satisfiable,
    _flatten_and_conditions,
    iter_rungs,
)
from pyrung.core.validation.display import FindingDisplay, Frame, _FindingTextMixin
from pyrung.core.validation.render import (
    caret_of,
    render_condition,
    render_rung_args,
    with_rung_line,
)
from pyrung.core.validation.sat import (
    conjunction_satisfiable,
    disjunction_tautological,
    negate_leaf,
)
from pyrung.core.validation.severity import Severity

if TYPE_CHECKING:
    from collections.abc import Sequence

    from pyrung.core.condition import Condition
    from pyrung.core.program import Program
    from pyrung.core.validation.context import ValidationContext

RUNG_CONTRADICTION = "RUNG_CONTRADICTION"
RUNG_TAUTOLOGY = "RUNG_TAUTOLOGY"
RUNG_REDUNDANT_TERM = "RUNG_REDUNDANT_TERM"

# Condition rendering now lives in ``render.py`` (shared with the CMP validator).

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
    return f"Or({', '.join(render_condition(t) for t in terms)})"


# ---------------------------------------------------------------------------
# Messages
# ---------------------------------------------------------------------------


def _contradiction_display(conds: Sequence[Condition], loc: RungLoc) -> FindingDisplay:
    header = with_rung_line(conds)
    span = caret_of(header, render_rung_args(conds))
    frame_caret = (0, span[0], span[1]) if span else None
    label = "can't both be true" if _blocking_pair(conds) is not None else "never true"
    dm = _demorgan_hint(conds)
    return FindingDisplay(
        code=RUNG_CONTRADICTION,
        severity="error",
        frames=(
            Frame(location=loc.compact, lines=(header,), caret=frame_caret, caret_label=label),
        ),
        hint=f"did you mean {dm}?" if dm else "",
    )


def _tautology_display(
    conds: Sequence[Condition],
    taut: Sequence[Condition],
    residual: Sequence[Condition],
    loc: RungLoc,
) -> FindingDisplay:
    header = with_rung_line(conds)
    span = caret_of(header, render_condition(taut[0]))
    frame_caret = (0, span[0], span[1]) if span else None
    if residual:
        hint = f"drop it; the real gate is {render_rung_args(residual)}"
    else:
        hint = "drop it; nothing else gates this rung"
    return FindingDisplay(
        code=RUNG_TAUTOLOGY,
        severity="warning",
        frames=(
            Frame(
                location=loc.compact, lines=(header,), caret=frame_caret, caret_label="always true"
            ),
        ),
        hint=hint,
    )


def _redundant_display(
    conds: Sequence[Condition], redundant: Condition, loc: RungLoc
) -> FindingDisplay:
    header = with_rung_line(conds)
    token = render_condition(redundant)
    span = caret_of(header, token)
    return FindingDisplay(
        code=RUNG_REDUNDANT_TERM,
        severity="info",
        frames=(
            Frame(
                location=loc.compact,
                lines=(header,),
                caret=(0, span[0], span[1]) if span else None,
                caret_label="redundant term" if span else "",
            ),
        ),
        hint=f"remove {token}; it does not change this condition",
    )


def _implies(left: Condition, right: Condition, domains: DomainMap) -> bool:
    left_parts = _comparison_parts(left)
    right_parts = _comparison_parts(right)
    if left_parts is not None and right_parts is not None and left_parts[0] == right_parts[0]:
        name = left_parts[0]
        if name in domains:
            satisfying_left = [
                value for value in domains[name] if _comparison_holds(left_parts, value)
            ]
            return bool(satisfying_left) and all(
                _comparison_holds(right_parts, value) for value in satisfying_left
            )
        return _open_domain_comparison_implies(left_parts, right_parts)
    negated = negate_leaf(right)
    return negated is not None and not _conjunction_satisfiable((left, negated), domains)


_COMPARE_OPERATOR: dict[type[Any], str] = {
    CompareEq: "==",
    CompareNe: "!=",
    CompareGt: ">",
    CompareGe: ">=",
    CompareLt: "<",
    CompareLe: "<=",
}


def _comparison_parts(condition: Condition) -> tuple[str, str, int | float] | None:
    if not isinstance(condition, tuple(_COMPARE_OPERATOR)):
        return None
    tag = condition.tag.value if isinstance(condition.tag, ImmediateRef) else condition.tag
    value = condition.value.value if isinstance(condition.value, ImmediateRef) else condition.value
    if not isinstance(tag, Tag) or isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return tag.name, _COMPARE_OPERATOR[type(condition)], value


def _comparison_holds(parts: tuple[str, str, int | float], value: int | float) -> bool:
    _, operator, bound = parts
    return {
        "==": value == bound,
        "!=": value != bound,
        ">": value > bound,
        ">=": value >= bound,
        "<": value < bound,
        "<=": value <= bound,
    }[operator]


def _open_domain_comparison_implies(
    left: tuple[str, str, int | float],
    right: tuple[str, str, int | float],
) -> bool:
    _, left_op, left_bound = left
    _, right_op, right_bound = right
    if left_op == "==":
        return _comparison_holds(right, left_bound)
    if left_op == "!=" or right_op in ("==", "!="):
        return False
    if left_op in (">", ">=") and right_op in (">", ">="):
        if left_bound > right_bound:
            return True
        return left_bound == right_bound and not (left_op == ">=" and right_op == ">")
    if left_op in ("<", "<=") and right_op in ("<", "<="):
        if left_bound < right_bound:
            return True
        return left_bound == right_bound and not (left_op == "<=" and right_op == "<")
    return False


def _or_tautological(terms: Sequence[Condition], domains: DomainMap) -> bool:
    negated = [negate_leaf(term) for term in terms]
    return (
        bool(negated)
        and all(term is not None for term in negated)
        and not (
            _conjunction_satisfiable(tuple(term for term in negated if term is not None), domains)
        )
    )


def _redundant_terms(
    terms: Sequence[Condition], *, conjunction: bool, domains: DomainMap
) -> tuple[Condition, ...]:
    """Direct duplicate/subsumed terms for one And/Or connective."""
    if len(terms) < 2:
        return ()
    if conjunction and not _conjunction_satisfiable(terms, domains):
        return ()
    if not conjunction and _or_tautological(terms, domains):
        return ()

    redundant: set[int] = set()
    rendered = [render_condition(term) for term in terms]
    for right_index in range(len(terms)):
        for left_index in range(right_index):
            if rendered[left_index] == rendered[right_index]:
                redundant.add(right_index)
                break

    for left_index, left in enumerate(terms):
        for right_index, right in enumerate(terms):
            if left_index == right_index or left_index in redundant or right_index in redundant:
                continue
            left_implies_right = _implies(left, right, domains)
            if not left_implies_right:
                continue
            right_implies_left = _implies(right, left, domains)
            if right_implies_left:
                redundant.add(max(left_index, right_index))
            elif conjunction:
                redundant.add(right_index)
            else:
                redundant.add(left_index)
    return tuple(terms[index] for index in sorted(redundant))


def _nested_redundant_terms(
    conditions: Sequence[Condition], domains: DomainMap
) -> tuple[Condition, ...]:
    found: list[Condition] = list(_redundant_terms(conditions, conjunction=True, domains=domains))

    def visit(condition: Condition) -> None:
        if isinstance(condition, AllCondition):
            found.extend(_redundant_terms(condition.conditions, conjunction=True, domains=domains))
            for child in condition.conditions:
                visit(child)
        elif isinstance(condition, AnyCondition):
            found.extend(_redundant_terms(condition.conditions, conjunction=False, domains=domains))
            for child in condition.conditions:
                visit(child)

    for condition in conditions:
        visit(condition)
    return tuple(dict.fromkeys(found))


# ---------------------------------------------------------------------------
# Public types
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class RungConditionFinding(_FindingTextMixin):
    """A rung-level satisfiability finding (contradiction or tautology)."""

    code: str
    target_name: str
    display: FindingDisplay
    severity: Severity

    @property
    def message(self) -> str:
        return self.display.as_text()


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


def validate_rung_conditions(
    program: Program,
    *,
    _context: ValidationContext | None = None,
) -> RungConditionReport:
    """Validate rung conditions for contradictions and tautological Or terms.

    One pass emitting three codes:

    * ``RUNG_CONTRADICTION`` when a rung's condition conjunction is provably
      unsatisfiable (skips the intentional bare ``rung()``).
    * ``RUNG_TAUTOLOGY`` when a top-level ``Or`` conjunct is provably always-true.
    * ``RUNG_REDUNDANT_TERM`` for duplicate or subsumed And/Or terms.

    Both grades of a single rung are reported: the buggy guard rung is both a
    contradiction and carries a tautological Or term.
    """
    from pyrung.core.validation.context import ValidationContext

    context = _context or ValidationContext(program)
    domains: DomainMap = {
        name: set(values)
        for name, values in context.closed_domains.items()
        if all(isinstance(value, (int, float)) for value in values)
    }
    findings: list[RungConditionFinding] = []

    for loc, rung in iter_rungs(program):
        conds = tuple(rung._conditions)
        if not conds:
            continue  # bare rung() — the intentional always-on rung

        if not conjunction_satisfiable(conds):
            findings.append(
                RungConditionFinding(
                    code=RUNG_CONTRADICTION,
                    target_name=loc.compact,
                    display=_contradiction_display(conds, loc),
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
                    target_name=loc.compact,
                    display=_tautology_display(conds, taut, residual, loc),
                    severity="warning",
                )
            )

        for redundant in _nested_redundant_terms(conds, domains):
            findings.append(
                RungConditionFinding(
                    code=RUNG_REDUNDANT_TERM,
                    target_name=loc.compact,
                    display=_redundant_display(conds, redundant, loc),
                    severity="info",
                )
            )

    return RungConditionReport(findings=tuple(findings))
