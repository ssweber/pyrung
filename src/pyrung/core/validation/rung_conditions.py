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

from pyrung.core.condition import AnyCondition
from pyrung.core.validation._common import RungLoc, _flatten_and_conditions, iter_rungs
from pyrung.core.validation.display import FindingDisplay, Frame
from pyrung.core.validation.render import (
    caret_of,
    render_condition,
    render_rung_args,
    with_rung_line,
)
from pyrung.core.validation.sat import (
    conjunction_satisfiable,
    disjunction_tautological,
)
from pyrung.core.validation.severity import Severity

if TYPE_CHECKING:
    from collections.abc import Sequence

    from pyrung.core.condition import Condition
    from pyrung.core.program import Program

RUNG_CONTRADICTION = "RUNG_CONTRADICTION"
RUNG_TAUTOLOGY = "RUNG_TAUTOLOGY"

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
        hint = f"drop it — the real gate is {render_rung_args(residual)}"
    else:
        hint = "drop it — nothing else gates this rung"
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


# ---------------------------------------------------------------------------
# Public types
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class RungConditionFinding:
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

    return RungConditionReport(findings=tuple(findings))
