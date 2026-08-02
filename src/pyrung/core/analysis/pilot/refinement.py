"""Bounded evidence refinement for corrective investigation.

Relational refinement re-solves an unresolved correction against successive
counterexample snapshots. Pinned suppression probes nominate finite Bool
levers when static guard analysis cannot classify a live-word antagonist.
Both paths are evidence readers: they consume explicit budgets, return
hypotheses or nominations, and never install a correction or drive PILOT.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from pyrung.core.analysis.pilot.constrained_reachability import (
    FrontierStatus,
    NoRoute,
    Reachable,
)
from pyrung.core.analysis.pilot.corrections import (
    CorrectionHypothesis,
    refine_relational_hypothesis,
)
from pyrung.core.analysis.pilot.overlay import PilotRung
from pyrung.core.analysis.pilot.skiff import run_pinned_scan
from pyrung.core.analysis.sp_values import _values_match

_RELATIONAL_REFINEMENT_BUDGET = 32

# Pinned escalation for a live-word-gated antagonist.
_SKIFF_SCANS = 4  # pulse -> staged register -> gated clobber, all in one window
_SKIFF_MAX_PROBES = 8  # bounded per incident; forks are cheap, not free

ActionPair = tuple[str, Any]
RefinementIdentity = Callable[[Iterable[Any]], tuple[tuple[str, Any], ...]]
RelationalRefiner = Callable[
    [CorrectionHypothesis, Mapping[str, Any], Any], CorrectionHypothesis | None
]
PinnedScan = Callable[..., Any]


@dataclass
class _RelationalRefinementReceipt:
    """Bounded counterexample refinements, independent of causal closure."""

    budget: int = _RELATIONAL_REFINEMENT_BUDGET
    refinements: int = 0
    seen: set[tuple[Any, ...]] = field(default_factory=set)

    def admit(self, identity: tuple[Any, ...]) -> bool:
        if identity in self.seen or self.refinements >= self.budget:
            return False
        self.seen.add(identity)
        self.refinements += 1
        return True

    @property
    def exhausted(self) -> bool:
        return self.refinements >= self.budget


def _continuation_ground(status: FrontierStatus) -> str:
    """Render the exact static ground carried by a continuation verdict."""

    if isinstance(status, Reachable):
        return ", ".join(status.provenance)
    if isinstance(status, NoRoute):
        return status.proof
    return status.reason


def _refine_unknown_continuation(
    candidate: CorrectionHypothesis,
    replay_outcome: Any,
    ctx: Any,
    receipt: _RelationalRefinementReceipt,
    *,
    identity: RefinementIdentity,
    refiner: RelationalRefiner = refine_relational_hypothesis,
) -> tuple[CorrectionHypothesis | None, str]:
    """Produce one new relational candidate or an honest terminal ground."""

    refinement_snap = replay_outcome.continuation_snapshot or replay_outcome.snapshot
    refined = refiner(candidate, refinement_snap, ctx)
    if refined is None:
        return (
            None,
            "relational continuation remains Unknown and yielded no new authoritative operand",
        )
    fingerprint = identity(refined.holds)
    if receipt.admit(fingerprint):
        return refined, ""
    if receipt.exhausted:
        return (
            None,
            "relational continuation remains Unknown after exhausting "
            f"{receipt.budget} counterexample refinements",
        )
    return (
        None,
        "relational continuation remains Unknown and repeated a prior counterexample refinement",
    )


def _skiff_suppression_nominations(
    work: Any,
    tag: str,
    desired: Any,
    node: Any,
    applied_actions: Sequence[ActionPair],
    pdg: Any,
    steerable: frozenset[str],
    pilot_rungs: Sequence[PilotRung],
    *,
    run_pinned: PinnedScan = run_pinned_scan,
) -> list[ActionPair]:
    """Return bounded pinned-probe nominations for a live-word antagonist.

    Only condition-read Bool levers are flipped. Results remain nominations
    and must pass investigation's ordinary replay and confirmation gates.
    """

    action_tags = {action_tag for action_tag, _ in applied_actions}
    condition_read = {
        read_tag
        for rung_node in pdg.rung_nodes
        for read_tag in getattr(rung_node, "condition_reads", ())
    }
    cone: set[str] = set()
    for guard_tag in node.condition_reads:
        cone |= set(pdg.upstream_slice(guard_tag, follow_calls=True))
        cone.add(guard_tag)
    levers = sorted((cone & steerable & condition_read) - action_tags)

    snap = dict(work.state.tags)
    allowed = set(pdg.upstream_slice(tag, follow_calls=True))
    allowed.add(tag)
    allowed.update(action_tags)

    nominations: list[ActionPair] = []
    budget = _SKIFF_MAX_PROBES
    for lever in levers:
        if budget <= 0:
            break
        current = snap.get(lever)
        if not isinstance(current, bool):
            continue
        budget -= 1
        value = not current
        probe_actions = tuple({**dict(applied_actions), lever: value}.items())
        result = run_pinned(
            work,
            frozenset(allowed | {lever}),
            pdg,
            pilot_rungs=pilot_rungs,
            actions=probe_actions,
            scans=_SKIFF_SCANS,
        )
        if _values_match(result.after.get(tag), desired):
            nominations.append((lever, value))
    return nominations
