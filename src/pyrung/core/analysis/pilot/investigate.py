"""Bounded incident investigation for PILOT regressions.

``pilot.py`` decides that the vessel left the bearing.  This module owns the
separate question: what hypotheses are worth replaying, and which ones survive
counterfactual validation?
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from types import SimpleNamespace
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
from pyrung.core.analysis.pilot.causal import chase_cause_roots, chase_chain_tags
from pyrung.core.analysis.pilot.corrections import break_guard_holds, correct_enablers
from pyrung.core.analysis.pilot.sandbox import run_pinned_scan
from pyrung.core.analysis.pilot.trace import _can_produce, trace_back
from pyrung.core.analysis.pilot.types import BearingDeparture, DeviationIncident
from pyrung.core.analysis.sp_values import _values_match, _written_value_for_tag

if TYPE_CHECKING:
    from pyrung.core.analysis.pdg import ProgramGraph
    from pyrung.core.analysis.pilot.trace import DomainPrior, TraceChoice
    from pyrung.core.runner import PLC

logger = logging.getLogger(__name__)

_DEPARTURE_MARGIN = 10

# Skiff escalation for a live-word-gated antagonist (excursion suppression).
_SKIFF_SCANS = 4  # pulse -> staged register -> gated clobber, all in one window
_SKIFF_MAX_PROBES = 8  # bounded per-excursion — forks are cheap, not free

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
    route: TraceChoice | None,
    prior: DomainPrior | None = None,
    clear_only: frozenset[str] = frozenset(),
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
        # One shared registry: _install_holds now rebuilds the probe's steady-hold
        # rungs from the registry it is given, so the two installs must accumulate
        # into the same dict (separate temp dicts would make the second rebuild
        # drop the first's holds).
        probe_registry: dict[str, Any] = {}
        _install_holds(probe, list(all_holds_steady.items()), probe_registry)
        _install_holds(probe, list(holds), probe_registry)
        # Conditional holds (from forced holds + this hypothesis) animate during
        # the coast; they are never forced steady.  Merge rule-wise (not dict
        # replace) so a hypothesis adding the complementary liveness polarity
        # oscillates against the already-held one instead of overwriting it.
        _, hyp_conditional = _split_holds(list(holds))
        conditional = dict(base_conditional)
        for tag, ch in hyp_conditional.items():
            conditional[tag] = _merge_hold(conditional.get(tag), ch)
        for step in steps:
            if step.inputs:
                _apply_pulse(probe, list(step.inputs.items()), resting, edge_tags)
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
            clear_only=clear_only,
            opaque_loop=opaque_loop,
            pipeline_internal_tags=pipeline_internal_tags,
            route=route,
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

    Primary path: suppress the *antagonist* — any writer of a reverted register
    that is **causally implicated** in the deviation (``cause()`` attributes the
    tag's change to it) and that **provably drives the tag away** from the value
    the pulse established (``_can_produce`` False).  Dispatch is by causal
    implication + producibility, never by instruction class name: a plain
    clobbering ``copy`` is suppressed exactly like a ``reset``.  Each implicated
    writer's guard is forced FALSE by the inverted-polarity forcing enumeration
    (``break_guard_holds``); when that punts on a genuinely-live word guard, the
    skiff runs bounded isolated probes for a suppressing lever (nominations only).

    Fallback: cause-chain walk and cause() enablers (original path — this is what
    resolves seal-in establishment cases, where the writer *can* still produce the
    desired value so it is not a suppression antagonist).
    """
    from pyrung.core.analysis.pdg import resolve_rung

    reverted: list[str] = []
    for i, name in enumerate(cfg.stateful_names):
        if i in cfg.acc_indices:
            continue
        if not _values_match(pre_snap.get(name), post_pulse_snap.get(name)):
            reverted.append(name)

    candidate_holds: list[ActionPair] = []
    seen: set[ActionPair] = set()

    # Antagonist suppression path: for each reverted register, suppress any writer
    # that is causally implicated in the deviation and provably clobbers the value
    # the pulse established.  Guard-force enumeration first; skiff on a live-word
    # punt.  Every hold is confirmed by the retry gate below — nothing unverified.
    if pdg is not None and program is not None:
        settled_snap = dict(fork.state.tags)
        mini_ctx = SimpleNamespace(
            pdg=pdg,
            program=program,
            steerable=steerable,
            opaque_loop=frozenset(),
            pipeline_internal_tags=frozenset(),
            route=None,
            domain_prior=None,
            nd_domains=None,
        )
        for tag in reverted:
            desired = post_pulse_snap.get(tag)
            for ni in _implicated_writers(fork, tag, pdg, program):
                node = pdg.rung_nodes[ni]
                ro = resolve_rung(program, node)
                if ro is None:
                    continue
                # Honesty boundary (mirrors trace's ``_preserve_children``): only
                # suppress a writer that *provably* drives the tag off the desired
                # value.  A writer that could still produce it (the seal-in OTE)
                # is an establishment case for the fallback, not a clobberer.
                if _can_produce(_written_value_for_tag(ro, tag), desired):
                    continue
                holds = break_guard_holds(ro, settled_snap, mini_ctx)
                if holds is None:
                    # Live-word guard: enumeration punted -> isolated skiff probes.
                    holds = _skiff_suppression_nominations(
                        work, tag, desired, node, action, pdg, steerable
                    )
                for hold in holds or ():
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


def _implicated_writers(plc: PLC, tag: str, pdg: Any, program: Any) -> list[int]:
    """PDG writer-node indices of *tag* causally implicated in its deviation.

    Dispatch by causal implication, never by instruction class: ``cause()``
    attributes the reverted tag's change to the rung(s) that actually wrote it in
    the settled window; those are the antagonists worth suppressing.  A writer
    that never fired is not in the chain and is left alone.  Maps the chain's
    ``(rung_index, subroutine)`` back to the PDG writer nodes.  ``[]`` when
    ``cause()`` is unavailable — the fallback path then runs unchanged.
    """
    try:
        chain = plc.cause(tag)
    except Exception:  # noqa: BLE001
        chain = None
    if chain is None:
        return []
    implicated = {
        (step.rung_index, step.subroutine)
        for step in chain.steps
        if step.transition.tag_name == tag
    }
    if not implicated:
        return []
    out: list[int] = []
    for ni in pdg.writers_of.get(tag, frozenset()):
        node = pdg.rung_nodes[ni]
        if (node.rung_index, node.subroutine) in implicated:
            out.append(ni)
    return out


def _skiff_suppression_nominations(
    work: PLC,
    tag: str,
    desired: Any,
    node: Any,
    action: list[ActionPair],
    pdg: Any,
    steerable: frozenset[str],
) -> list[ActionPair]:
    """Bounded isolated probes for a live-word-gated antagonist — nominations only.

    ``break_guard_holds`` punted (the antagonist's guard reads a genuinely-live
    word with no forceable finite domain).  Probe each **condition-read**
    steerable Bool lever in the antagonist guard's upstream cone: hold it, replay
    the pulse in a pinned fork over the deviation window (``run_pinned_scan``),
    and keep the levers under which the antagonist does **not** fire — the reverted
    register ends at its desired (pulse-established) value.

    Only Bool levers are probed (flipped off their current antagonist-firing
    value); a wide/unknown word offers no sound probe value (the skiff never
    guesses).  The returned holds are nominations: they ride the same retry gate
    as any static hold and are never applied unconfirmed.
    """
    action_tags = {t for t, _ in action}
    condition_read = {t for n in pdg.rung_nodes for t in getattr(n, "condition_reads", ())}
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
        cur = snap.get(lever)
        if not isinstance(cur, bool):
            continue  # only Bool levers — never guess a word value
        budget -= 1
        val = not cur  # flip off the polarity under which the antagonist fires
        probe_actions = tuple({**dict(action), lever: val}.items())
        result = run_pinned_scan(
            work, frozenset(allowed | {lever}), pdg, actions=probe_actions, scans=_SKIFF_SCANS
        )
        if _values_match(result.after.get(tag), desired):
            nominations.append((lever, val))
    return nominations


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
    program: Any = None,
    governing_tag: str | None = None,
) -> DeviationIncident:
    """Capture the facts inside the known off-course window.

    *program*, when given, narrows ``changed_tags`` to the fix engine's actual
    universe — every profile's Done bit (a mid-window pulse matters: a watchdog
    can fire then reset, which is exactly the complement-reset oscillation
    ``correct_enablers`` looks for) and accumulator register (the only tags
    membership is ever tested against).  Diffing that handful instead of the
    whole register file is the difference between O(window x all-tags) and
    O(window x profiles).  ``None`` keeps the full diff (direct callers / tests).
    """
    relevant: frozenset[str] | None = None
    if program is not None:
        relevant = frozenset(
            name for p, _ in iter_profiles(program) for name in (p.done.name, p.accumulator.name)
        )
    changed_tags = _changed_tags_in_window(plc, anchor_scan, end_scan, relevant)
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
        governing_tag=governing_tag,
    )


# ---------------------------------------------------------------------------
# Investigation engine
# ---------------------------------------------------------------------------


def _hold_is_noop(tag: str, value: Any, snap: Mapping[str, Any], pdg: Any, program: Any) -> bool:
    """A hold that changes nothing cannot be a correction.

    Pinning *tag* at a value it already holds is inert when no program writer
    can move it off that value (every writer stamps a literal matching it —
    the clear-only idiom: holding ``Heat_xPause`` at its rest 0 counters
    nothing, because the program only ever writes 0).  A FREEZE survives this
    test: it either drives the tag OFF its current value or pins against a
    writer that can produce a different one.  Oscillating (``ConditionalHold``)
    values are never no-ops.
    """
    from pyrung.core.analysis.pdg import resolve_rung
    from pyrung.core.analysis.pilot.trace import _literal_write

    if getattr(value, "rules", None) is not None:
        return False
    if not _values_match(snap.get(tag), value):
        return False
    for ri in pdg.writers_of.get(tag, frozenset()):
        ro = resolve_rung(program, pdg.rung_nodes[ri])
        if ro is None:
            return False  # unreadable writer — assume it could move the tag
        lw = _literal_write(ro, tag)
        if lw is None or not _values_match(lw, value):
            return False  # a write that can move the tag — the hold pins something
    return True


def _rank_hypotheses(
    plc: PLC,
    hypotheses: Sequence[InvestigationHypothesis],
    incident: DeviationIncident,
    ctx: Any,
) -> list[InvestigationHypothesis]:
    """Order competing hypotheses by **causal primacy**, not generation order.

    The governing departure (``incident.governing_tag`` — the ejection itself)
    is the incident; other departures are collateral downstream of it (the
    state-8 shared-init resetting ``Heat_CurStep``).  Two primacy signals,
    strongest first:

    * **chain membership** — the hypothesis's tags sit inside the cause chain
      of the governing departure.  Right when the chain is readable; today the
      recorded-history walk dead-ends at the opaque pipeline
      (``S_StateRequested`` / ``isStateEnbl_Yes``), so on a PackML-shaped
      program it stops short of the watchdog.  The jump table itself IS
      inverted elsewhere (``table_oracle`` / ``evidence.expand_routes``) —
      bridging the chain across the pipeline hop with those routes is the
      open follow-up that would let this signal reach the root directly.
    * **temporal precedence** — how close the hypothesis's most recent source
      transition sits to the governing departure scan.  Pure scan-log
      observation, no inversion: the ejecting watchdog's Done rises *at* the
      ejection; a bystander (``Test_Simulate_1st_Scan``'s alarm timer) fired
      somewhere earlier in a 1000-scan coast, and a collateral symptom
      (``Heat_CurStep`` at 1810 vs the ejection at 1855) trails by the same
      measure.

    Ties break by lightest intervention, then generation order.
    """
    gov = incident.governing_tag
    dep_scan = {d.tag: d.scan for d in incident.departures if d.scan is not None}
    primal: set[str] = set()
    gov_scan = incident.end_scan
    if gov is not None:
        if dep_scan.get(gov) is not None:
            gov_scan = dep_scan[gov]
        # All tags on the chain, not just steerable roots: an absence-caused
        # ejection (a sensor that never moved) has no steerable mover at all.
        primal = {gov} | chase_chain_tags(plc, gov, scan=dep_scan.get(gov))

    big = 1 << 30

    def _proximity(tags: set[str]) -> int:
        best = big
        for t in tags:
            last = _last_transition_scan(plc, t, incident.anchor_scan, gov_scan)
            if last is not None:
                best = min(best, gov_scan - last)
        return best

    def _key(pair: tuple[int, InvestigationHypothesis]) -> tuple[int, int, int, int]:
        idx, h = pair
        tags = set(h.sources) | {t for t, _ in h.holds}
        in_chain = 0 if (primal and tags & primal) else 1
        proximity = 0 if in_chain == 0 else _proximity(tags)
        return (in_chain, proximity, len(h.holds), idx)

    return [h for _, h in sorted(enumerate(hypotheses), key=_key)]


def investigate_deviation(
    plc: PLC,
    incident: DeviationIncident,
    ctx: Any,
    replay: ReplayFn,
    *,
    needed: Sequence[tuple[str, Any]] = (),
    installed: Mapping[str, Any] | None = None,
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

    Hypotheses are **competing explanations of one incident, not a bundle of
    independent fixes**: they are ranked by causal primacy
    (:func:`_rank_hypotheses`) and the FIRST hypothesis that survives the
    static self-defeat check (*needed* — the checkpoint frontier) and the
    replay is confirmed **alone**.  A union of individually-replayed holds is
    an untested configuration — installing exactly one keeps the installed set
    exactly what was replayed.  Hypotheses whose holds are *already installed*
    (*installed*) are skipped, not re-confirmed: they were active when the
    incident happened, so a repeat regression at the same key escalates to the
    runner-up instead of re-anointing the incumbent.
    """
    raw: list[InvestigationHypothesis] = []
    precise = _precise_cause(plc, incident, ctx)
    if precise is not None:
        raw.append(precise)
    raw.extend(
        InvestigationHypothesis(kind=c.kind, holds=c.holds, sources=c.sources, detail=c.detail)
        for c in correct_enablers(plc, incident, ctx)
    )
    hypotheses = _rank_hypotheses(plc, _dedupe_hypotheses(raw), incident, ctx)
    confirmed: list[InvestigationHypothesis] = []
    rejected: list[InvestigationHypothesis] = []
    confirmed_holds: list[ActionPair] = []
    pdg = getattr(ctx, "pdg", None)
    program = getattr(ctx, "program", None)

    for hypothesis in hypotheses:
        if not hypothesis.holds:
            rejected.append(hypothesis)
            continue
        if installed and all(
            ht in installed and (installed[ht] == hv or _values_match(installed[ht], hv))
            for ht, hv in hypothesis.holds
        ):
            continue  # active when the incident happened — escalate past it
        if (
            pdg is not None
            and program is not None
            and all(
                _hold_is_noop(ht, hv, incident.before_snap, pdg, program)
                for ht, hv in hypothesis.holds
            )
        ):
            # Every hold pins a value already in place that the program cannot
            # move — the "correction" changes nothing, so its replay pass is
            # vacuous and installing it burns the round on a byte-identical
            # re-coast.
            rejected.append(hypothesis)
            continue
        if (
            needed
            and pdg is not None
            and program is not None
            and any(
                hold_defeats_needed(ht, hv, needed, pdg, program) for ht, hv in hypothesis.holds
            )
        ):
            rejected.append(hypothesis)
            continue
        outcome = replay(hypothesis.holds)
        if outcome.accepted:
            confirmed.append(hypothesis)
            confirmed_holds.extend(hypothesis.holds)
            break  # first confirmed wins — one intervention per incident
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


def _atom_true_under(atom: Any, value: Any) -> bool | None:
    """Whether a simplified ``Atom`` holds given its tag is steadily *value*.

    Returns ``None`` for an edge form (``rise``/``fall``) — a steadily held value
    never produces an edge, so it can never *force* an edge-gated rung.
    """
    form = atom.form
    if form in ("rise", "fall"):
        return None
    if form in ("xic", "truthy"):
        return bool(value)
    if form == "xio":
        return not bool(value)
    op = atom.operand
    if form == "eq":
        return _values_match(value, op)
    if form == "ne":
        return not _values_match(value, op)
    try:
        if form == "lt":
            return value < op
        if form == "le":
            return value <= op
        if form == "gt":
            return value > op
        if form == "ge":
            return value >= op
    except TypeError:
        return None
    return None


def _expr_forced_true(expr: Any, assign: dict[str, Any]) -> bool | None:
    """Three-valued: is *expr* **necessarily** True under partial *assign*?

    Tags absent from *assign* are UNKNOWN.  ``True`` means the expression holds
    regardless of the unknowns (an ``Or`` with one satisfied disjunct, an ``And``
    whose every term is satisfied); ``None`` means it depends on the unknowns.
    """
    from pyrung.core.analysis.simplified import And, ArithAtom, Atom, Const, Or

    if isinstance(expr, Const):
        return expr.value
    if isinstance(expr, Atom):
        return None if expr.tag not in assign else _atom_true_under(expr, assign[expr.tag])
    if isinstance(expr, ArithAtom):
        return None
    if isinstance(expr, And):
        vals = [_expr_forced_true(t, assign) for t in expr.terms]
        if any(v is False for v in vals):
            return False
        return True if all(v is True for v in vals) else None
    if isinstance(expr, Or):
        vals = [_expr_forced_true(t, assign) for t in expr.terms]
        if any(v is True for v in vals):
            return True
        return False if all(v is False for v in vals) else None
    return None


def _hold_values(hold_value: Any) -> tuple[Any, ...]:
    """The steady values a hold can pin its tag to: a scalar hold is that value;
    a ``ConditionalHold`` contributes each of its rule target values (an
    oscillation reaches each of them)."""
    rules = getattr(hold_value, "rules", None)
    if rules is not None:
        return tuple(r.value for r in rules)
    return (hold_value,)


def hold_defeats_needed(
    tag: str, hold_value: Any, needed: Sequence[tuple[str, Any]], pdg: Any, program: Any
) -> bool:
    """Whether holding *tag* at *hold_value* is **self-defeating**.

    Held steady, ``tag == value`` is true every scan, so any rung that value alone
    *forces* to fire runs every scan.  If such a rung writes a register the target
    still *needs* (``needed`` = the checkpoint frontier's outstanding ``(tag,
    value)`` pairs) to a literal contradicting the needed value, the hold pins
    that register away from the goal forever and the coast can never reach the
    target — e.g. ``Heat_xInit=1`` forces the shared-init rung that fills
    ``Heat_CurStep := 1`` while the target needs ``Heat_CurStep = 3``.
    Purely static (no long coast), name-free (dispatches on write-vs-need).

    ``needed`` is ordered target-most first (``frontier_pairs`` walks the tree
    breadth-first), so for a stepping register the *first* value per tag is the
    requirement and deeper values are en-route stopovers (``Heat_CurStep`` needs
    ``[3, 2, 1]``: 3 is the goal, 1 and 2 are how it gets there).  A steady
    forced write pins the register at one value, so it must satisfy the
    shallowest need — a write matching only a deeper stopover (``fill(1, …)``
    against a needed 3) still pins progress short of the goal and defeats.
    """
    from pyrung.core.analysis.pdg import resolve_rung
    from pyrung.core.analysis.pilot.trace import _literal_write
    from pyrung.core.analysis.simplified import _conditions_list_to_expr

    needed_first: dict[str, Any] = {}
    for nt, nv in needed:
        needed_first.setdefault(nt, nv)
    if not needed_first:
        return False
    values = _hold_values(hold_value)
    for node in pdg.rung_nodes:
        if tag not in getattr(node, "condition_reads", ()):
            continue
        ro = resolve_rung(program, node)
        if ro is None:
            continue
        expr = _conditions_list_to_expr(getattr(ro, "_conditions", []))
        if not any(_expr_forced_true(expr, {tag: v}) is True for v in values):
            continue
        for nt, first_need in needed_first.items():
            wv = _literal_write(ro, nt)
            if wv is not None and not _values_match(wv, first_need):
                return True
    return False


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


def _changed_tags_in_window(
    plc: PLC,
    start_scan: int,
    end_scan: int,
    relevant: frozenset[str] | None = None,
) -> tuple[str, ...]:
    """Tags whose value changed between any adjacent pair in the window.

    *relevant* restricts the diff to a candidate universe.  The incident's
    changed set is only ever queried for membership of profile Done bits and
    accumulator registers (``correct_enablers`` / ``incident_eject_dones``), so
    the caller passes that handful of tags rather than paying an
    O(window x whole-register-file) diff.  ``None`` diffs every tag (full
    generality — used by the direct unit test).
    """
    try:
        states = plc.history.range(start_scan, end_scan + 1)
    except Exception:  # noqa: BLE001
        return ()
    changed: set[str] = set()
    for prev, cur in zip(states, states[1:], strict=False):
        tags = (set(prev.tags) | set(cur.tags)) if relevant is None else relevant
        changed.update(
            tag for tag in tags if not _values_match(prev.tags.get(tag), cur.tags.get(tag))
        )
    return tuple(sorted(changed))


def _last_transition_scan(
    plc: PLC,
    tag: str,
    start_scan: int,
    end_scan: int,
) -> int | None:
    """The latest scan in the window where *tag* changed value, or ``None``.

    The temporal-precedence signal for hypothesis ranking: the watchdog Done
    that ejected the bearing rises *at* the governing departure; a bystander
    fired somewhere earlier in a long coast window.
    """
    try:
        states = plc.history.range(start_scan, end_scan + 1)
    except Exception:  # noqa: BLE001
        return None
    last: int | None = None
    for prev, cur in zip(states, states[1:], strict=False):
        if not _values_match(prev.tags.get(tag), cur.tags.get(tag)):
            last = cur.scan_id
    return last


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
