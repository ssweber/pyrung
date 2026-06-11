"""The walker's pass registry: declared static advice, run once per walk.

Mirrors ``prove/``'s pass idiom at walk scale: registered passes run once at
``plan_walk`` entry against ``(program, pdg)`` only, freeze their advice into
the per-walk context, and journal what applied so diagnosis (Stage D4) can
report which advice shaped the walk.  Passes have no handle to the agenda,
the work fork, or the learning stores — the only door into the loop is the
frozen :class:`_WalkAdvice`, so a heuristic physically cannot become a
parallel path.

Each pass declares its **kind**, and the kind is its proof obligation
(enforced by the ablation matrix in ``test_walk_passes.py``):

- ``ordering`` — advice about what to try first.  Disabling changes only
  effort (recovery iters, forks), never verdicts.
- ``narrowing`` — advice about what to skip.  Must be conservative
  (over-approximate): disabling only widens the search, so verdicts are
  preserved up to budget exhaustion.
- ``fold`` — widens time-fold availability (plateau-guard exclusions /
  crossing sources).  Each fold pass carries its own exactness argument
  (e.g. unread ⇒ unobservable), and the step-by-step verify replay backstops
  all of them: a wrong fold yields a plan that fails verification, never a
  wrong plan.  Disabling restores the stricter plateau guard, so verdicts on
  churn-free programs are unchanged and programs that needed the fold regress
  only in the refusing direction (honest unreachable / budget exhaustion).

Runtime learning (nogoods, holds) stays out of the registry — everything
load-bearing lives on the loop side of the line; that is what keeps the
ablation property true.  Soundness is untouched either way: replay
verification carries it, so no pass can break it.  Passes touch
completeness only.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from pyrung.core.analysis.pdg import ProgramGraph


@dataclass(frozen=True)
class _WalkPass:
    """A registered advice pass: a name, its kind, and its ablation story."""

    name: str
    kind: str  # "ordering" | "narrowing"
    description: str


# The registry.  Every row gets an ablation-matrix case by construction;
# new advice must land here (with a kind) before the loop may consult it.
WALK_PASSES: tuple[_WalkPass, ...] = (
    _WalkPass(
        "cone_filter",
        "narrowing",
        "Limit steer candidates to external inputs in the governing tag's "
        "upstream cone; disabled, every external Bool input is a candidate.",
    ),
    _WalkPass(
        "steer_polarity",
        "narrowing",
        "Emit only the steer forms the enabling conditions need per input "
        "(pulse for positive forms, low for negated); disabled, every "
        "candidate gets both pulse and low.",
    ),
    _WalkPass(
        "helpful_order",
        "ordering",
        "Try inputs named by the governing value's enabling conditions "
        "before the rest of the cone; disabled, candidates stay in sorted "
        "order.",
    ),
    _WalkPass(
        "set_value_relevance",
        "narrowing",
        "Keep set-value steers for non-Bool ND inputs named by the "
        "governing value's enabling conditions (and the governing tag "
        "itself) at full domain; the remaining in-cone ND inputs fill a "
        "bounded remainder (cap _MAX_SET_VALUE_STEERS).  Disabled, every "
        "in-cone ND input contributes its full domain — on wide programs "
        "that is hundreds of steers paid at every explore node.",
    ),
    _WalkPass(
        "fold_unread_churn",
        "fold",
        "Exclude unread self-updating calc tags (per-scan churn with no "
        "readers outside their own writer rungs) from the plateau guard; "
        "disabled, such churn defeats time-folding program-wide.",
    ),
    _WalkPass(
        "fold_disjoint_churn",
        "fold",
        "Exclude read churn whose downstream cone is fully disjoint from "
        "the walk's target cones (churner plus everything its readers "
        "write, transitively) from the plateau guard; disabled, such churn "
        "defeats time-folding program-wide.",
    ),
    _WalkPass(
        "fold_modwrap_source",
        "fold",
        "Track unconditional affine(-mod) self-calc churn as an exact fold "
        "source: excluded from the plateau guard, patched in closed form "
        "during jumps, its read comparisons joining the crossing set; "
        "disabled, such churn defeats time-folding program-wide.",
    ),
    _WalkPass(
        "fold_derived_crossings",
        "fold",
        "Translate thresholds read through an unconditional copy/constant-"
        "offset mirror of a fold source onto the source itself (X = Acc + k "
        "makes 'X cmp T' flip at 'Acc cmp T-k') and drop the mirror from "
        "the plateau guard; mirrors with any unresolvable read are refused; "
        "disabled, acc-mirror churn defeats time-folding program-wide.",
    ),
)

_PASS_NAMES: frozenset[str] = frozenset(p.name for p in WALK_PASSES)
_VALID_KINDS: frozenset[str] = frozenset({"ordering", "narrowing", "fold"})


@dataclass(frozen=True)
class _PassDecision:
    """One journaled pass decision (the D4 Diagnosis feed)."""

    pass_name: str
    kind: str
    outcome: str  # "active" | "disabled"
    reason: str
    detail: tuple[tuple[str, Any], ...] = ()


@dataclass
class _WalkJournal:
    """Per-walk journal: pass decisions plus free-form context notes."""

    decisions: list[_PassDecision] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    def add_note(self, note: str) -> None:
        self.notes.append(note)

    def __str__(self) -> str:
        lines: list[str] = []
        for d in self.decisions:
            lines.append(f"[{d.pass_name}] {d.kind} -> {d.outcome}: {d.reason}")
            for k, v in d.detail:
                lines.append(f"  {k}: {v}")
        for note in self.notes:
            lines.append(f"note: {note}")
        return "\n".join(lines)


@dataclass(frozen=True)
class _WalkAdvice:
    """Frozen advice handle the loop consults; built once per walk.

    An absent handle (``advice=None`` at the consuming sites) means
    all-enabled — the pre-registry behavior, preserved bit-identically.
    """

    enabled: frozenset[str] = _PASS_NAMES

    def has(self, name: str) -> bool:
        return name in self.enabled


def run_walk_passes(
    program: Any,
    pdg: ProgramGraph,
    *,
    disabled: frozenset[str] = frozenset(),
) -> tuple[_WalkAdvice, _WalkJournal]:
    """Run the registry once against ``(program, pdg)``; freeze the advice.

    *disabled* names passes to ablate (matrix-test hook).  Unknown names
    raise — a typo silently enabling everything would defeat the matrix.
    """
    unknown = disabled - _PASS_NAMES
    if unknown:
        raise ValueError(f"unknown walk pass(es): {sorted(unknown)}")
    del program, pdg  # advice is currently knob-shaped; builders journal data
    journal = _WalkJournal()
    enabled: set[str] = set()
    for p in WALK_PASSES:
        if p.name in disabled:
            journal.decisions.append(
                _PassDecision(p.name, p.kind, "disabled", "ablated for this walk")
            )
            continue
        enabled.add(p.name)
        journal.decisions.append(_PassDecision(p.name, p.kind, "active", p.description))
    return _WalkAdvice(enabled=frozenset(enabled)), journal
