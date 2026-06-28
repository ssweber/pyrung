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
    LivenessHold,
    _apply_pulse,
    _coast_holding_state,
    _coast_to_value,
    _install_holds,
    _pilot_state_key,
    _settle_delayed_effects,
    _split_holds,
)
from pyrung.core.analysis.pilot.causal import chase_cause_roots
from pyrung.core.analysis.pilot.trace import trace_back
from pyrung.core.analysis.sp_values import _values_match

if TYPE_CHECKING:
    from pyrung.core.analysis.pdg import ProgramGraph
    from pyrung.core.analysis.pilot.trace import TraceChoice
    from pyrung.core.runner import PLC

logger = logging.getLogger(__name__)

_DEPARTURE_MARGIN = 10

ActionPair = tuple[str, Any]
ReplayFn = Callable[[tuple[ActionPair, ...]], "ReplayOutcome"]


# ---------------------------------------------------------------------------
# Incident / hypothesis / result types
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class BearingDeparture:
    """One fact that held at the incident anchor and later departed."""

    tag: str
    value: Any
    scan: int | None


@dataclass(frozen=True)
class DeviationIncident:
    """The bounded window where verify observed a loss of bearing."""

    anchor_scan: int
    departure_scan: int | None
    end_scan: int
    action: tuple[ActionPair, ...]
    bearing: tuple[ActionPair, ...]
    before_snap: Mapping[str, Any]
    after_snap: Mapping[str, Any]
    changed_tags: tuple[str, ...]
    departures: tuple[BearingDeparture, ...]


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
    zoom_governing_tag: str | None = None,
    zoom_target_value: Any = None,
    terminal_letrun_role_tags: tuple[str, ...] | None = None,
    departure_scan: int | None = None,
    departure_bearing: tuple[tuple[str, Any], ...] = (),
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
    """

    all_holds_steady = {**forced_holds}
    _, base_liveness = _split_holds(list(forced_holds.items()))

    def _replay(holds: tuple[ActionPair, ...]) -> ReplayOutcome:
        probe = cp_fork.fork()
        _install_holds(probe, list(all_holds_steady.items()), {})
        _install_holds(probe, list(holds), {})
        # Liveness holds (from forced holds + this hypothesis) animate during the
        # coast; they are never forced steady.
        _, hyp_liveness = _split_holds(list(holds))
        liveness = {**base_liveness, **hyp_liveness}
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
                    liveness=liveness,
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
            return ReplayOutcome(
                accepted=reached,
                trend=None,
                snapshot=snap,
                reason=f"{zoom_governing_tag} -> {zoom_target_value!r} reached={reached}",
            )

        # Terminal let-run without a governing register (no recognized state
        # machine): judge the global target at the bounded point.
        if terminal_letrun_role_tags is not None:
            reached = _values_match(snap.get(target_tag), target_value)
            return ReplayOutcome(
                accepted=reached,
                trend=None,
                snapshot=snap,
                reason=f"{target_tag} -> {target_value!r} reached={reached}",
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
) -> ExcursionResult:
    """Diagnose an excursion and replay-validate candidate holds.

    Verify detected that the state key changed during the pulse but
    reverted after settling.  This function finds *why* (cause-chain
    roots of the revert) and *validates* (fork, install holds, re-pulse,
    check if the key sticks).
    """
    reverted: list[str] = []
    for i, name in enumerate(cfg.stateful_names):
        if i in cfg.acc_indices:
            continue
        if not _values_match(pre_snap.get(name), post_pulse_snap.get(name)):
            reverted.append(name)

    candidate_holds: list[ActionPair] = []
    seen: set[ActionPair] = set()
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
    """Investigate an incident by proposing hypotheses and replaying them."""
    raw: list[InvestigationHypothesis] = []
    raw.extend(_cause_hypotheses(plc, incident, ctx))
    raw.extend(_latch_exposure_hypotheses(plc, incident, ctx))
    raw.extend(_liveness_hypotheses(plc, incident, ctx))
    raw.extend(_upstream_hypotheses(incident, ctx))
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
# Hypothesis generation
# ---------------------------------------------------------------------------


def _cause_hypotheses(
    plc: PLC,
    incident: DeviationIncident,
    ctx: Any,
) -> list[InvestigationHypothesis]:
    """Plain-as-day path: recorded cause names transitioning steerable roots."""
    hypotheses: list[InvestigationHypothesis] = []
    for departure in incident.departures:
        nogoods, holds = chase_cause_roots(plc, departure.tag, ctx.steerable, scan=departure.scan)
        holds_filtered = tuple(pair for pair in _dedupe_pairs(holds) if _hold_allowed(ctx, pair))
        if holds_filtered:
            hypotheses.append(
                InvestigationHypothesis(
                    kind="recorded-cause",
                    holds=holds_filtered,
                    sources=tuple(sorted(nogoods | {departure.tag})),
                    detail=f"{departure.tag} departed at scan {departure.scan}",
                )
            )
    return hypotheses


def _latch_exposure_hypotheses(
    plc: PLC,
    incident: DeviationIncident,
    ctx: Any,
) -> list[InvestigationHypothesis]:
    """Latch-exposure: alarm latches that fired as a consequence of our action.

    A latch that is *active* (True after the regression) and *gated by a state
    we were already in* (True in ``before_snap``) latched because of the move we
    made into that state — the door/lint alarms latch the instant we enter
    Starting.  Each such latch's non-state guard inputs are preconditions we
    failed to establish; we flip each to the value that breaks the latch and
    resolve it to its steerable driver via ``trace_back`` (bridging the
    ``i_DoorClosed`` PIVOT to the physical ``x_DoorClosed``).

    The holds are proposed both per-latch *and* as one conjunction: when several
    alarms fire together (door AND lint), no single hold reaches the corridor —
    only clearing every active latch does.
    """
    from pyrung.core.analysis.pdg import resolve_rung
    from pyrung.core.instruction.coils import LatchInstruction

    pdg = getattr(ctx, "pdg", None)
    steerable = getattr(ctx, "steerable", frozenset())
    opaque_loop = getattr(ctx, "opaque_loop", frozenset())
    program = getattr(ctx, "program", None)
    if pdg is None or program is None:
        return []
    pipeline_internal = getattr(ctx, "pipeline_internal_tags", frozenset())
    choice = getattr(ctx, "choice", None)

    def _steerable_holds(guard: str, safe: Any) -> list[ActionPair]:
        """Resolve guard=safe to (steerable_input, value) holds."""
        if guard in steerable:
            return [(guard, safe)]
        try:
            tree = trace_back(
                guard,
                safe,
                dict(incident.after_snap),
                pdg,
                program,
                steerable,
                opaque_loop=opaque_loop,
                pipeline_internal_tags=pipeline_internal,
                choice=choice,
            )
        except Exception:  # noqa: BLE001
            return []
        return list(tree.steerable_leaves())

    def _latch_guard_holds(tag: str) -> list[ActionPair]:
        """Corrective steerable holds for an active latch *tag*, or []."""
        holds: list[ActionPair] = []
        seen: set[ActionPair] = set()
        for ri in pdg.writers_of.get(tag, frozenset()):
            node = pdg.rung_nodes[ri]
            ro = resolve_rung(program, node)
            if ro is None or not any(isinstance(i, LatchInstruction) for i in ro._instructions):
                continue
            # The PDG node's condition_reads is subroutine-aware; the resolved
            # rung's sp_tree() has no tag-name accessor.  Polarity is irrelevant
            # here — we flip each guard off its current value and let the replay
            # judge — so the read set is all we need.
            condition_tags = set(node.condition_reads)
            state_tags = condition_tags & opaque_loop
            # Fired on our action only if gated by a state we were already in.
            if not any(_values_match(incident.before_snap.get(s), True) for s in state_tags):
                continue
            for guard in sorted(condition_tags - state_tags):
                cur = incident.after_snap.get(guard)
                if not isinstance(cur, bool):
                    continue
                for hold in _steerable_holds(guard, not cur):
                    if hold not in seen and _hold_allowed(ctx, hold):
                        seen.add(hold)
                        holds.append(hold)
        return holds

    hypotheses: list[InvestigationHypothesis] = []
    conjunction: list[ActionPair] = []
    conj_seen: set[ActionPair] = set()
    conj_latches: list[str] = []
    for tag, val in incident.after_snap.items():
        if val is not True:
            continue
        latch_holds = _latch_guard_holds(tag)
        if not latch_holds:
            continue
        hypotheses.append(
            InvestigationHypothesis(
                kind="latch-exposure",
                holds=tuple(latch_holds),
                sources=(tag, *(h[0] for h in latch_holds)),
                detail=f"latch {tag} active in entered state",
            )
        )
        conj_latches.append(tag)
        for hold in latch_holds:
            if hold not in conj_seen:
                conj_seen.add(hold)
                conjunction.append(hold)

    if len(conjunction) > 1:
        hypotheses.append(
            InvestigationHypothesis(
                kind="latch-exposure",
                holds=tuple(conjunction),
                sources=(*conj_latches, *(h[0] for h in conjunction)),
                detail=f"clear {len(conj_latches)} active latches: {', '.join(conj_latches)}",
            )
        )
    return hypotheses


def _liveness_hypotheses(
    plc: PLC,
    incident: DeviationIncident,
    ctx: Any,
) -> list[InvestigationHypothesis]:
    """Liveness: a watchdog tripped because a sensor input sat still.

    A *complement-reset watchdog* is an ``on_delay`` whose ``.reset()`` is driven
    by an input — ``rotate.py`` R10/R11: ``SensorOnWD`` resets on ``~sensor``,
    ``SensorOffWD`` on ``sensor``.  Held at either polarity too long, the timer
    completes and its Done bit ejects the SFC (``Rotate_Error`` -> Aborting).  A
    steady hold can never satisfy it; the input must *oscillate*.

    Detection is structural and program-agnostic: among the timers whose Done bit
    fired during this incident, resolve each reset input to its steerable physical
    driver (``i_RotateSensor`` -> ``x_RotateSensor`` via ``trace_back``), and
    propose a :class:`LivenessHold` whose dwell is half the shortest such
    watchdog preset — so neither polarity outlasts any watchdog on that input,
    whichever edge resets it.  The replay confirms or rejects it.
    """
    from pyrung.core.analysis.pdg import _extract_reads_from_condition
    from pyrung.core.instruction.timers import OnDelayInstruction
    from pyrung.core.validation._common import walk_instructions

    pdg = getattr(ctx, "pdg", None)
    steerable = getattr(ctx, "steerable", frozenset())
    opaque_loop = getattr(ctx, "opaque_loop", frozenset())
    program = getattr(ctx, "program", None)
    if pdg is None or program is None:
        return []
    pipeline_internal = getattr(ctx, "pipeline_internal_tags", frozenset())
    choice = getattr(ctx, "choice", None)

    changed = set(incident.changed_tags)
    after = dict(incident.after_snap)
    dt = float(getattr(plc, "_dt", 0.0)) or 0.01

    def _resolve_input(tag: str) -> str | None:
        """Bridge a reset-condition tag to its steerable physical driver."""
        if tag in steerable:
            return tag
        try:
            tree = trace_back(
                tag,
                True,
                after,
                pdg,
                program,
                steerable,
                opaque_loop=opaque_loop,
                pipeline_internal_tags=pipeline_internal,
                choice=choice,
            )
        except Exception:  # noqa: BLE001
            return None
        leaves = list(tree.steerable_leaves())
        return leaves[0][0] if leaves else None

    # Two passes over every complement-reset watchdog in the program:
    #   shortest_preset[phys] — min scans-to-done of ANY watchdog whose reset
    #     reads this input.  The toggle introduces BOTH polarities, so the dwell
    #     must clear the tightest watchdog on the input, not just the one that
    #     fired (rotate: SensorOnWD 2s vs SensorOffWD 10s — toggling at 10s/2
    #     would trip the 2s one).
    #   fired — inputs whose watchdog actually completed in this incident; only
    #     these get a hypothesis (keeps it incident-relevant).
    shortest_preset: dict[str, int] = {}
    fired: set[str] = set()
    for instr in walk_instructions(program):
        if not isinstance(instr, OnDelayInstruction) or instr.reset_condition is None:
            continue
        reset_reads = _extract_reads_from_condition(instr.reset_condition, {})
        if not reset_reads:
            continue
        units_per_scan = instr.unit.dt_to_units(dt)
        scans = (
            int(instr.preset / units_per_scan)
            if isinstance(instr.preset, int) and units_per_scan > 0
            else 0
        )
        did_fire = instr.done_bit.name in changed
        for rtag in reset_reads:
            phys = _resolve_input(rtag)
            if phys is None or not _hold_allowed(ctx, (phys, True)):
                continue
            if scans > 0:
                prev = shortest_preset.get(phys)
                shortest_preset[phys] = scans if prev is None else min(prev, scans)
            if did_fire:
                fired.add(phys)

    hypotheses: list[InvestigationHypothesis] = []
    for phys in sorted(fired):
        scans = shortest_preset.get(phys, 0)
        dwell = max(2, scans // 2) if scans > 0 else 50
        lh = LivenessHold(on_dwell=dwell, off_dwell=dwell)
        hypotheses.append(
            InvestigationHypothesis(
                kind="liveness",
                holds=((phys, lh),),
                sources=(phys,),
                detail=f"oscillate {phys} every {dwell} scans (complement-reset watchdog)",
            )
        )
    return hypotheses


def _upstream_hypotheses(
    incident: DeviationIncident,
    ctx: Any,
) -> list[InvestigationHypothesis]:
    """Heuristic upstream: steerable inputs in the PDG cone of departed tags.

    For each departure, find steerable inputs in the upstream cone and propose
    holds for replay to test.  The pre-incident value alone only ever reverts a
    *transition*; a precondition that was never satisfied (e.g. a door that was
    never closed) has no good past value to restore.  So for boolean inputs we
    propose *both* polarities and let the replay decide — the steady-but-wrong
    case is fixed by the flipped value, the transitioned case by the original.
    """
    pdg = getattr(ctx, "pdg", None)
    steerable = getattr(ctx, "steerable", frozenset())
    if pdg is None or not steerable:
        return []

    hypotheses: list[InvestigationHypothesis] = []
    seen: set[ActionPair] = set()
    for departure in incident.departures:
        try:
            cone = pdg.upstream_slice(departure.tag)
        except Exception:  # noqa: BLE001
            continue
        candidates = cone & steerable
        for st in sorted(candidates):
            before_val = incident.before_snap.get(st)
            values: list[Any] = [before_val]
            if isinstance(before_val, bool):
                # The corrective polarity — restores a never-satisfied
                # precondition that the pre-incident value can't.
                values.append(not before_val)
            for val in values:
                hold = (st, val)
                if hold in seen:
                    continue
                seen.add(hold)
                if _hold_allowed(ctx, hold):
                    hypotheses.append(
                        InvestigationHypothesis(
                            kind="heuristic-upstream",
                            holds=(hold,),
                            sources=(departure.tag, st),
                            detail=f"{st} in upstream cone of {departure.tag}",
                        )
                    )
    return hypotheses


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


def _route_allowed(ctx: Any, pair: ActionPair) -> bool:
    route_allowed = getattr(ctx, "route_allowed", None)
    return bool(route_allowed(pair)) if route_allowed is not None else True


def _hold_allowed(ctx: Any, pair: ActionPair) -> bool:
    tag, _value = pair
    compass = getattr(ctx, "compass", None)
    action_tags = getattr(compass, "action_tags", frozenset())
    return tag not in action_tags and _route_allowed(ctx, pair)


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
