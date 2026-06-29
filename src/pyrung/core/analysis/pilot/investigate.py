"""Bounded incident investigation for PILOT regressions.

``pilot.py`` decides that the vessel left the bearing.  This module owns the
separate question: what hypotheses are worth replaying, and which ones survive
counterfactual validation?
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from pyrung.core.analysis.pilot._ops import (
    _ZOOM_BUDGET,
    _apply_pulse,
    _coast_holding_state,
    _coast_to_value,
    _hold_allowed,
    _install_holds,
    _merge_hold,
    _pilot_state_key,
    _settle_delayed_effects,
    _split_holds,
)
from pyrung.core.analysis.pilot.accumulators import iter_profiles
from pyrung.core.analysis.pilot.causal import chase_cause_roots
from pyrung.core.analysis.pilot.corrections import correct_enablers
from pyrung.core.analysis.pilot.trace import trace_back
from pyrung.core.analysis.pilot.types import BearingDeparture, DeviationIncident
from pyrung.core.analysis.sp_values import _values_match

if TYPE_CHECKING:
    from pyrung.core.analysis.pdg import ProgramGraph
    from pyrung.core.analysis.pilot.trace import DomainPrior, TraceChoice
    from pyrung.core.runner import PLC

logger = logging.getLogger(__name__)

_DEPARTURE_MARGIN = 10

ActionPair = tuple[str, Any]
ReplayFn = Callable[[tuple[ActionPair, ...]], "ReplayOutcome"]


# ---------------------------------------------------------------------------
# Incident / hypothesis / result types
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class InvestigationHypothesis:
    """A replay-testable explanation for an incident."""

    kind: str
    holds: tuple[ActionPair, ...]
    sources: tuple[str, ...] = ()
    detail: str = ""


@dataclass(frozen=True)
class ReplayOutcome:
    """Pilot's replay judgment for a proposed hold set."""

    accepted: bool
    trend: int | None
    snapshot: Mapping[str, Any]
    reason: str = ""


@dataclass(frozen=True)
class InvestigationResult:
    """Replay-confirmed corrective information."""

    confirmed_holds: tuple[ActionPair, ...] = ()
    regression_nogoods: frozenset[ActionPair] = frozenset()
    hypotheses: tuple[InvestigationHypothesis, ...] = ()
    confirmed: tuple[InvestigationHypothesis, ...] = ()
    rejected: tuple[InvestigationHypothesis, ...] = ()
    unresolved: tuple[str, ...] = ()


# ---------------------------------------------------------------------------
# Replay harness — fork, hold, replay steps, trace-back, judge
# ---------------------------------------------------------------------------


def incident_eject_dones(incident: DeviationIncident, program: Any) -> frozenset[str]:
    """Accumulator ``Done`` bits that fired inside the incident window.

    These are the watchdogs whose completion ejected PILOT.  Passed to
    :func:`build_replay_fn` so a hold that silences one of them but trips a
    *different* watchdog is scored as new-cause progress, not a rejection.
    """
    changed = set(incident.changed_tags)
    return frozenset(p.done.name for p, _ in iter_profiles(program) if p.done.name in changed)


def build_replay_fn(
    cp_fork: PLC,
    cp_trend: int,
    forced_holds: dict[str, Any],
    steps: Sequence[Any],
    *,
    resting: dict[str, Any],
    edge_tags: set[str],
    target_tag: str,
    target_value: Any,
    pdg: ProgramGraph,
    program: Any,
    steerable: frozenset[str],
    opaque_loop: frozenset[str],
    pipeline_internal_tags: frozenset[str],
    choice: TraceChoice | None,
    prior: DomainPrior | None = None,
    zoom_governing_tag: str | None = None,
    zoom_target_value: Any = None,
    terminal_letrun_role_tags: tuple[str, ...] | None = None,
    departure_scan: int | None = None,
    departure_bearing: tuple[tuple[str, Any], ...] = (),
    eject_cause_dones: frozenset[str] = frozenset(),
) -> ReplayFn:
    """Build a replay callback for ``investigate_deviation``.

    The returned function forks from the checkpoint, installs existing holds
    plus the proposed hypothesis holds, and re-runs the act that surfaced the
    regression.

    The judgment depends on the incident shape:

    * **Governed incident** (``zoom_governing_tag`` set — a zoom corridor or a
      terminal let-run holding a macro-state) — a hold is *good* iff the
      governing register sits at its target/held value instead of ejecting.  The
      coast differs by shape: a **zoom** coast is unbounded and ejection-guarded
      (the corridor target is a full coast away), a **let-run** coast is
      **bounded** to the departure window (its far-off global target is
      unreachable inside it).  In both cases the bearing's far-off conjuncts (the
      governing target, the global target, unrelated watch tags) are *not*
      required — only that the register did not eject — because a bounded coast
      cannot restore them and the bearing-held test would reject every hold.
    * **Terminal let-run without a governing register** — judge the global
      target at the bounded point.
    * **Command incident** — judge *departure_bearing* directly, else fall back
      to comparing the trace-back trend against the checkpoint trend.

    *New-cause progress* (``eject_cause_dones``): a governed/let-run hold that
    still ejects is normally rejected, but a one-sided liveness hold *fixes its
    own watchdog and trips the complement* — it must not be rejected for the
    complement's ejection, or round-by-round can never accumulate the second
    polarity.  So if the replay silenced an original ejecting watchdog Done bit
    and now ejects on a *different* accumulator Done, accept it as progress; the
    complement's ejection is the next round's incident.
    """
    all_done_tags = frozenset(p.done.name for p, _ in iter_profiles(program))

    all_holds_steady = {**forced_holds}
    _, base_conditional = _split_holds(list(forced_holds.items()))

    def _replay(holds: tuple[ActionPair, ...]) -> ReplayOutcome:
        probe = cp_fork.fork()
        _install_holds(probe, list(all_holds_steady.items()), {})
        _install_holds(probe, list(holds), {})
        # Conditional holds (from forced holds + this hypothesis) animate during
        # the coast; they are never forced steady.  Merge rule-wise (not dict
        # replace) so a hypothesis adding the complementary liveness polarity
        # oscillates against the already-held one instead of overwriting it.
        _, hyp_conditional = _split_holds(list(holds))
        conditional = dict(base_conditional)
        for tag, ch in hyp_conditional.items():
            conditional[tag] = _merge_hold(conditional.get(tag), ch)
        for step in steps:
            if step.action:
                _apply_pulse(probe, list(step.action.items()), resting, edge_tags)
            elif terminal_letrun_role_tags is not None:
                budget = (
                    max(1, departure_scan - probe.state.scan_id + _DEPARTURE_MARGIN)
                    if departure_scan is not None
                    else _ZOOM_BUDGET
                )
                _coast_holding_state(
                    probe,
                    target_tag,
                    target_value,
                    terminal_letrun_role_tags,
                    conditional=conditional,
                    budget=budget,
                )
            elif zoom_governing_tag is not None:
                # Coast to the corridor target under the ejection guard.  Do NOT
                # bound this by the departure window: the governing register's
                # corridor target is the immediate goal but a full corridor coast
                # away (~the whole Starting->Execute completion), so a bounded
                # coast can never reach it.  The guard already stops at the first
                # ejection, so the coast is naturally bounded to *this* corridor.
                _coast_to_value(probe, zoom_governing_tag, zoom_target_value)
            else:
                for _ in range(max(1, step.scans)):
                    probe.step()
        snap = dict(probe.state.tags)

        def _new_cause(snap: Mapping[str, Any]) -> str | None:
            """Reason string if this replay ejected on a *different* watchdog than
            the incident — the one-sided liveness hold fixed its own watchdog and
            tripped the complement — else ``None``."""
            if not eject_cause_dones:
                return None
            firing = {d for d in all_done_tags if snap.get(d) is True}
            silenced = eject_cause_dones - firing
            new = firing - eject_cause_dones
            if silenced and new:
                return (
                    f"new-cause progress: silenced {sorted(silenced)}, now ejects on {sorted(new)}"
                )
            return None

        # Governed incident (zoom corridor OR terminal let-run hold): the hold is
        # good iff the governing register sits at its target/held value instead of
        # ejecting — *reached* for a zoom corridor, *maintained* for a let-run
        # hold.  Either way the bearing's far-off conjuncts (the governing target
        # itself, the global target, unrelated watch tags) must NOT be required:
        # a bounded coast cannot restore them, so the bearing-held test would
        # reject every hold — including the latch-clears / liveness holds that
        # actually fix the ejection.  Ask the direct question against the
        # governing register instead.  The coast already differs by shape: the
        # zoom coast is unbounded and ejection-guarded (the corridor target is a
        # full coast away); the let-run coast is bounded to the departure window
        # (its global target is unreachable inside it).
        if zoom_governing_tag is not None:
            reached = _values_match(snap.get(zoom_governing_tag), zoom_target_value)
            progressed = _new_cause(snap) if not reached else None
            return ReplayOutcome(
                accepted=reached or progressed is not None,
                trend=None,
                snapshot=snap,
                reason=progressed
                or f"{zoom_governing_tag} -> {zoom_target_value!r} reached={reached}",
            )

        # Terminal let-run without a governing register (no recognized state
        # machine): judge the global target at the bounded point.
        if terminal_letrun_role_tags is not None:
            reached = _values_match(snap.get(target_tag), target_value)
            progressed = _new_cause(snap) if not reached else None
            return ReplayOutcome(
                accepted=reached or progressed is not None,
                trend=None,
                snapshot=snap,
                reason=progressed or f"{target_tag} -> {target_value!r} reached={reached}",
            )

        # Command incident: no register to coast toward — judge the bounded
        # bearing-held directly.
        if departure_bearing:
            held = all(_values_match(snap.get(t), v) for t, v in departure_bearing)
            return ReplayOutcome(
                accepted=held,
                trend=None,
                snapshot=snap,
                reason=f"bearing {'held' if held else 'departed'} at bounded replay",
            )

        tree = trace_back(
            target_tag,
            target_value,
            snap,
            pdg,
            program,
            steerable,
            opaque_loop=opaque_loop,
            pipeline_internal_tags=pipeline_internal_tags,
            choice=choice,
            prior=prior,
        )
        trend = tree.unsatisfied_count()
        return ReplayOutcome(
            accepted=trend <= cp_trend,
            trend=trend,
            snapshot=snap,
            reason=f"trend {trend} <= checkpoint {cp_trend}",
        )

    return _replay


# ---------------------------------------------------------------------------
# Excursion investigation — verify detected a revert, investigate diagnoses
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ExcursionResult:
    """Replay-confirmed holds from an excursion investigation."""

    confirmed_holds: list[ActionPair]
    reverted: list[str]
    retry_fork: Any = None


def investigate_excursion(
    work: PLC,
    fork: PLC,
    pre_snap: dict[str, Any],
    post_pulse_snap: dict[str, Any],
    pre_key: tuple[Any, ...],
    action: list[ActionPair],
    *,
    cfg: Any,
    steerable: frozenset[str],
    forced_holds: dict[str, Any],
    resting: dict[str, Any],
    edge_tags: set[str],
    scan_budget: int,
    pdg: Any = None,
    program: Any = None,
) -> ExcursionResult:
    """Diagnose an excursion and replay-validate candidate holds.

    Verify detected that the state key changed during the pulse but
    reverted after settling.  This function finds *why* and *validates*.

    Primary path: find the *antagonist* — the reset/unlatch/OTE rung that
    undid the pulse — and trace its enable condition to steerable inputs.
    Same pattern as done-boundary: the antagonist instruction's condition
    reads are the lever, not the cause chain of the value change.

    Fallback: cause-chain walk and cause() enablers (original path).
    """
    from pyrung.core.analysis.pdg import resolve_rung
    from pyrung.core.instruction.coils import ResetInstruction

    reverted: list[str] = []
    for i, name in enumerate(cfg.stateful_names):
        if i in cfg.acc_indices:
            continue
        if not _values_match(pre_snap.get(name), post_pulse_snap.get(name)):
            reverted.append(name)

    candidate_holds: list[ActionPair] = []
    seen: set[ActionPair] = set()

    # Antagonist-condition path: find the rung that *undid* our progress
    # and trace its enable condition to steerable inputs.
    if pdg is not None and program is not None:
        settled_snap = dict(fork.state.tags)
        opaque_loop = frozenset()
        pipeline_internal = frozenset()
        for tag in reverted:
            for ri in pdg.writers_of.get(tag, frozenset()):
                node = pdg.rung_nodes[ri]
                ro = resolve_rung(program, node)
                if ro is None:
                    continue
                is_antagonist = any(
                    isinstance(instr, ResetInstruction) for instr in ro._instructions
                )
                if not is_antagonist:
                    continue
                for cond_tag in sorted(node.condition_reads):
                    if cond_tag not in steerable:
                        tree = trace_back(
                            cond_tag,
                            not settled_snap.get(cond_tag, False),
                            settled_snap,
                            pdg,
                            program,
                            steerable,
                            opaque_loop=opaque_loop,
                            pipeline_internal_tags=pipeline_internal,
                        )
                        for leaf_tag, leaf_val in tree.steerable_leaves():
                            hold = (leaf_tag, leaf_val)
                            if hold not in seen:
                                seen.add(hold)
                                candidate_holds.append(hold)
                    else:
                        cur = settled_snap.get(cond_tag)
                        if isinstance(cur, bool):
                            hold = (cond_tag, not cur)
                            if hold not in seen:
                                seen.add(hold)
                                candidate_holds.append(hold)

    # Fallback: cause-chain walk.
    if not candidate_holds:
        for tag in reverted:
            _, holds = chase_cause_roots(fork, tag, steerable)
            for h in holds:
                if h not in seen:
                    seen.add(h)
                    candidate_holds.append(h)

            try:
                chain = fork.cause(tag)
            except Exception:  # noqa: BLE001
                continue
            if chain is None:
                continue
            for step in chain.steps:
                for enabler in step.enablers:
                    if enabler.tag_name not in steerable:
                        continue
                    if not isinstance(enabler.value, bool):
                        continue
                    hold = (enabler.tag_name, not enabler.value)
                    if hold not in seen:
                        seen.add(hold)
                        candidate_holds.append(hold)

    action_tags = {t for t, _ in action}
    candidate_holds = [(t, v) for t, v in candidate_holds if t not in action_tags]
    if not candidate_holds:
        return ExcursionResult(confirmed_holds=[], reverted=reverted)

    retry = work.fork()
    combined: dict[str, Any] = {}
    _install_holds(retry, list(forced_holds.items()), combined)
    _install_holds(retry, candidate_holds, combined)
    _apply_pulse(retry, list(action), resting, edge_tags)
    _settle_delayed_effects(retry, pre_snap, cfg, scan_budget=scan_budget)
    retry_snap = dict(retry.state.tags)
    retry_key = _pilot_state_key(retry_snap, cfg)

    if retry_key != pre_key:
        return ExcursionResult(
            confirmed_holds=candidate_holds,
            reverted=reverted,
            retry_fork=retry,
        )
    return ExcursionResult(confirmed_holds=[], reverted=reverted)


# ---------------------------------------------------------------------------
# Incident construction
# ---------------------------------------------------------------------------


def build_deviation_incident(
    plc: PLC,
    *,
    anchor_scan: int,
    end_scan: int,
    action: tuple[ActionPair, ...],
    bearing: tuple[ActionPair, ...],
    before_snap: Mapping[str, Any],
    after_snap: Mapping[str, Any],
) -> DeviationIncident:
    """Capture the facts inside the known off-course window."""
    changed_tags = _changed_tags_in_window(plc, anchor_scan, end_scan)
    departures = tuple(
        BearingDeparture(tag, value, _first_departure_scan(plc, tag, value, anchor_scan, end_scan))
        for tag, value in bearing
        if not _values_match(after_snap.get(tag), value)
    )
    departure_scans = [d.scan for d in departures if d.scan is not None]
    return DeviationIncident(
        anchor_scan=anchor_scan,
        departure_scan=min(departure_scans) if departure_scans else None,
        end_scan=end_scan,
        action=action,
        bearing=bearing,
        before_snap=before_snap,
        after_snap=after_snap,
        changed_tags=changed_tags,
        departures=departures,
    )


# ---------------------------------------------------------------------------
# Investigation engine
# ---------------------------------------------------------------------------


def investigate_deviation(
    plc: PLC,
    incident: DeviationIncident,
    ctx: Any,
    replay: ReplayFn,
) -> InvestigationResult:
    """Investigate an incident with precise hypothesis generation.

    Two sources, both instrument-derived:
    1. Precise cause walk — single cause()-chain from the first departure
       that reaches a steerable input (the *trigger-found* case).
    2. Enabler correction — when cause finds no steerable trigger, the held
       enablers are the cause; ``correct_enablers`` dispatches by writer
       instruction (coil latch -> FLIP guard, accumulator -> OSCILLATE /
       stop-hold).  Subsumes the former latch-exposure + done-boundary passes.
    No upstream cone sweep.
    """
    raw: list[InvestigationHypothesis] = []
    precise = _precise_cause(plc, incident, ctx)
    if precise is not None:
        raw.append(precise)
    raw.extend(
        InvestigationHypothesis(kind=c.kind, holds=c.holds, sources=c.sources, detail=c.detail)
        for c in correct_enablers(plc, incident, ctx)
    )
    hypotheses = _dedupe_hypotheses(raw)
    confirmed: list[InvestigationHypothesis] = []
    rejected: list[InvestigationHypothesis] = []
    confirmed_holds: list[ActionPair] = []

    for hypothesis in hypotheses:
        if not hypothesis.holds:
            rejected.append(hypothesis)
            continue
        outcome = replay(hypothesis.holds)
        if outcome.accepted:
            confirmed.append(hypothesis)
            confirmed_holds.extend(hypothesis.holds)
        else:
            rejected.append(hypothesis)

    return InvestigationResult(
        confirmed_holds=tuple(_dedupe_pairs(confirmed_holds)),
        regression_nogoods=frozenset(),
        hypotheses=tuple(hypotheses),
        confirmed=tuple(confirmed),
        rejected=tuple(rejected),
        unresolved=incident.changed_tags if not confirmed else (),
    )


# ---------------------------------------------------------------------------
# Hypothesis generation — precise pass
# ---------------------------------------------------------------------------


def _precise_cause(
    plc: PLC,
    incident: DeviationIncident,
    ctx: Any,
) -> InvestigationHypothesis | None:
    """Single cause()-chain walk from the first departure to a steerable input.

    Replaces the old ``_cause_hypotheses`` sweep: one walk, one hypothesis,
    early exit.  If no departure's cause chain reaches a steerable input,
    returns ``None``.
    """
    steerable = getattr(ctx, "steerable", frozenset())
    if not steerable:
        return None
    for departure in incident.departures:
        nogoods, holds = chase_cause_roots(plc, departure.tag, steerable, scan=departure.scan)
        holds_filtered = tuple(pair for pair in _dedupe_pairs(holds) if _hold_allowed(ctx, pair))
        if holds_filtered:
            return InvestigationHypothesis(
                kind="precise-cause",
                holds=holds_filtered,
                sources=tuple(sorted(nogoods | {departure.tag})),
                detail=f"{departure.tag} departed at scan {departure.scan}",
            )
    return None


# ---------------------------------------------------------------------------
# Hypothesis generation — structural
#
# The enabler-correction families (latch-exposure FLIP + accumulator
# OSCILLATE/stop-hold) live in ``corrections.py`` behind ``correct_enablers``,
# the single ``no-steerable-trigger -> corrective hold`` pass.
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _changed_tags_in_window(plc: PLC, start_scan: int, end_scan: int) -> tuple[str, ...]:
    try:
        states = plc.history.range(start_scan, end_scan + 1)
    except Exception:  # noqa: BLE001
        return ()
    changed: set[str] = set()
    for prev, cur in zip(states, states[1:], strict=False):
        tags = set(prev.tags) | set(cur.tags)
        changed.update(
            tag for tag in tags if not _values_match(prev.tags.get(tag), cur.tags.get(tag))
        )
    return tuple(sorted(changed))


def _first_departure_scan(
    plc: PLC,
    tag: str,
    value: Any,
    start_scan: int,
    end_scan: int,
) -> int | None:
    try:
        states = plc.history.range(start_scan, end_scan + 1)
    except Exception:  # noqa: BLE001
        return None
    for prev, cur in zip(states, states[1:], strict=False):
        if _values_match(prev.tags.get(tag), value) and not _values_match(cur.tags.get(tag), value):
            return cur.scan_id
    return None


def _dedupe_pairs(pairs: list[ActionPair]) -> list[ActionPair]:
    out: list[ActionPair] = []
    seen: set[ActionPair] = set()
    for pair in pairs:
        if pair in seen:
            continue
        seen.add(pair)
        out.append(pair)
    return out


def _dedupe_hypotheses(
    hypotheses: list[InvestigationHypothesis],
) -> tuple[InvestigationHypothesis, ...]:
    out: list[InvestigationHypothesis] = []
    seen: set[tuple[ActionPair, ...]] = set()
    for hypothesis in hypotheses:
        key = hypothesis.holds
        if key in seen:
            continue
        seen.add(key)
        out.append(hypothesis)
    return tuple(out)
