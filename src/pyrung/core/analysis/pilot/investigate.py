"""Build and replay bounded hypotheses for departures and excursions.

The module constructs incident windows and replay functions, derives candidate
holds from causal roots, writer enablers, and pinned scans, ranks those
hypotheses, and returns the first explanation that survives counterfactual
replay. It also provides the shorter excursion investigation used by trial
verification.

Investigation confirms a proposed correction but does not install it; recovery
and installation belong to ``progress.py``.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from itertools import product
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any

from pyrung.core.analysis.pilot._ops import (
    _ZOOM_BUDGET,
    PilotRung,
    _apply_pulse,
    _coast_holding_state,
    _coast_to_value,
    _hold_allowed,
    _pilot_state_key,
    _rungs_from_proposals,
    _set_rungs,
    _settle_delayed_effects,
    _target_unresolved_condition,
)
from pyrung.core.analysis.pilot.advance import iter_advance_owners
from pyrung.core.analysis.pilot.causal import (
    chase_cause_roots,
    chase_chain_tags,
    empirical_program_writes,
)
from pyrung.core.analysis.pilot.corrections import break_guard_holds, correct_enablers
from pyrung.core.analysis.pilot.skiff import run_pinned_scan
from pyrung.core.analysis.pilot.trace import _can_produce, trace_back
from pyrung.core.analysis.pilot.types import BearingDeparture, DeviationIncident
from pyrung.core.analysis.sp_values import (
    _SnapshotView,
    _values_match,
    _writer_for_tag,
    _written_value_for_tag,
)

if TYPE_CHECKING:
    from pyrung.core.analysis.pdg import ProgramGraph
    from pyrung.core.analysis.pilot.trace import DomainPrior, TraceChoice
    from pyrung.core.runner import PLC

logger = logging.getLogger(__name__)

# Skiff escalation for a live-word-gated antagonist (excursion suppression).
_SKIFF_SCANS = 4  # pulse -> staged register -> gated clobber, all in one window
_SKIFF_MAX_PROBES = 8  # bounded per-excursion — forks are cheap, not free

ActionPair = tuple[str, Any]


def _observe_stable_channel_landing(
    probe: PLC,
    channel_tag: str,
    *,
    settle: bool,
    session: Any = None,
) -> None:
    """Follow automatic motion beyond a replay-proved channel value.

    The incident window proves that a correction silenced the observed
    failure, but it can end while the PLC still sits at a commanded value.
    A raw hypothesis continues until the channel moves and then remains at one
    value long enough to reveal a stable landing. An already-scoped hypothesis
    stops at the first landing transition, exactly where the live loop regains
    control. If the channel never moves, leave the snapshot at the commanded
    value and let guarded replay fail closed.
    """
    from pyrung.core.analysis.pilot.coast import CoastSession, departure_bump

    commanded_value = probe.state.tags.get(channel_tag)
    if session is None:
        session = CoastSession(probe, kind="landing-observe")
    assert session.plc is probe
    scan_before = probe.state.scan_id
    receipt = session.seek(
        [departure_bump(probe, "moved", {channel_tag: commanded_value})],
        budget=_ZOOM_BUDGET,
    )
    if receipt.stop_reason != "departed":
        # Channel never moved: leave the snapshot at the commanded value and let
        # guarded replay fail closed — an honest non-landing, never a
        # settled one.
        return
    if not settle:
        return
    remaining = _ZOOM_BUDGET - (probe.state.scan_id - scan_before)
    if remaining > 0:
        session.settle_landing(channel_tag, cap=remaining)


def _proposal_pair(proposal: Any) -> ActionPair:
    if isinstance(proposal, PilotRung):
        return proposal.dest, proposal.value
    return proposal


ReplayFn = Callable[[tuple[Any, ...]], "ReplayOutcome"]


@dataclass(frozen=True)
class ReplayStep:
    """One recorded journey step with its session spec, replay-ready.

    ``kind`` is the *recorded* coast kind (``"pulse"`` / ``"zoom"`` /
    ``"letrun"`` / ``"dwell"``), read off the committed step context — never
    inferred from position or input emptiness.  A zoom step re-arms its own
    recorded ``channel_tag``/``channel_target``; a letrun step re-coasts
    toward the global target bounded by its own recorded span.
    """

    inputs: tuple[tuple[str, Any], ...]
    scans: int
    kind: str
    channel_tag: str | None = None
    channel_target: Any = None


# ---------------------------------------------------------------------------
# Incident / hypothesis / result types
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class InvestigationHypothesis:
    """A replay-testable explanation for an incident."""

    kind: str
    holds: tuple[Any, ...]
    sources: tuple[str, ...] = ()
    detail: str = ""


@dataclass(frozen=True)
class ReplayOutcome:
    """Pilot's replay judgment for a proposed hold set."""

    accepted: bool
    trend: int | None
    snapshot: Mapping[str, Any]
    reason: str = ""
    # Whether ``snapshot`` is a real LANDING (target reached, or the coast
    # departed and settled somewhere) rather than a mid-journey timeout.  A
    # departure-silenced acceptance times out with the channel intact — its
    # snapshot is where the budget ran out, not a destination, and channel
    # scoping must not derive a lifetime from it.
    landed: bool = True


@dataclass(frozen=True)
class InvestigationResult:
    """Replay-confirmed corrective information."""

    confirmed_holds: tuple[Any, ...] = ()
    regression_nogoods: frozenset[ActionPair] = frozenset()
    hypotheses: tuple[InvestigationHypothesis, ...] = ()
    confirmed: tuple[InvestigationHypothesis, ...] = ()
    # Every rejection retains the ground that made it fail.  A rejected
    # hypothesis without its ground is not useful evidence: it forces the
    # operator to reconstruct and re-run the incident.  ``rejected`` carries the
    # human ``(hypothesis, detail)`` pair; ``rejection_slugs`` is the parallel
    # (index-aligned) list of stable machine-readable ground slugs
    # ("no-holds", "vacuous-hold", "self-defeat", "exploratory-replay-failed",
    # "guarded-replay-failed") a consumer can classify without string-matching
    # the detail.  Built together, so element ``i`` of each always agree.
    rejected: tuple[tuple[InvestigationHypothesis, str], ...] = ()
    rejection_slugs: tuple[str, ...] = ()
    unresolved: tuple[str, ...] = ()


def _scoped_correction_rungs(
    plc: PLC,
    proposals: tuple[Any, ...],
    incident: DeviationIncident,
    outcome: ReplayOutcome,
    ctx: Any,
    progress_mark: tuple[tuple[str, Any], ...] = (),
) -> tuple[PilotRung, ...]:
    """Give a replay-successful correction its evidence-derived lifetime.

    The exploratory replay uses the global target boundary so it can discover
    where the corrected PLC naturally lands.  The installed form is then
    scoped from that observation and replayed *again* before confirmation:

    * motion to a different safe channel value -> remain active until that
      observed landing;
    * maintaining the source channel -> remain active while that source
      context holds;
    * no channel evidence -> the target-unresolved outer boundary.

    When the caller has an exact progress receipt, its source mark further
    narrows that lifetime.  A correction proved while the recipe sat at Step
    101, for example, cannot keep owning the same input after the recipe earns
    Step 103 without a new proof for that occurrence.

    Existing :class:`PilotRung` proposals already own their guards and pass
    through unchanged.
    """
    if all(isinstance(proposal, PilotRung) for proposal in proposals):
        return tuple(proposals)

    channel_tag = incident.channel_tag
    if (
        channel_tag is not None
        and outcome.landed
        and (channel := plc._known_tags_by_name.get(channel_tag)) is not None
    ):
        from pyrung.core.condition import CompareEq, CompareNe

        before = incident.before_snap.get(channel_tag)
        landing = outcome.snapshot.get(channel_tag)
        scope = (
            CompareEq(channel, before)
            if _values_match(landing, before)
            else CompareNe(channel, landing)
        )
    else:
        scope = _target_unresolved_condition(
            plc,
            ctx.target_tag,
            ctx.target_value,
            getattr(ctx, "target_predicate", None),
        )
    if progress_mark:
        from pyrung.core.condition import AllCondition, CompareEq

        coordinates = []
        for tag_name, value in progress_mark:
            tag = plc._known_tags_by_name.get(tag_name)
            if tag is None:
                raise KeyError(f"progress receipt tag {tag_name!r} is not a program tag")
            coordinates.append(CompareEq(tag, value))
        scope = AllCondition(scope, *coordinates)
    return tuple(_rungs_from_proposals(plc, list(proposals), scope))


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
    return frozenset(
        owner.profile.done.name
        for owner in iter_advance_owners(program)
        if owner.profile.done is not None and owner.profile.done.name in changed
    )


def incident_eject_latches(
    plc: PLC,
    incident: DeviationIncident,
    ctx: Any,
) -> tuple[tuple[str, Any], ...]:
    """Latched failures that became active inside the incident.

    A replay that keeps every such latch at its pre-incident value has removed
    the observed failure itself.  This is stronger evidence than landing on a
    declared route suffix and works for any latch-shaped PLC fault.

    The incident window supplies the observed transition; the enabler
    classifier supplies the semantic distinction between an antagonist latch
    and an intended progress latch.  Do not filter through ``chase_chain_tags``:
    an opaque state pipeline can truncate that walk before the alarm branch
    while still including an intended process latch (``Rotate_x``).  Either
    mistake erases the replay's proof that a partial correction removed this
    failure and exposed the next independent blocker.
    """
    exposed: set[str] = set()
    for correction in correct_enablers(plc, incident, ctx):
        if correction.kind != "latch-exposure":
            continue
        lever_tags = {_proposal_pair(hold)[0] for hold in correction.holds}
        for source in correction.sources:
            if (
                source not in lever_tags
                and incident.before_snap.get(source) is not True
                and incident.after_snap.get(source) is True
            ):
                exposed.add(source)
    protected = [
        (tag, incident.before_snap.get(tag)) for tag in incident.after_snap if tag in exposed
    ]
    logger.debug("incident causal eject latches: %s", protected)
    return tuple(protected)


def build_replay_fn(
    cp_fork: PLC,
    cp_trend: int,
    rungs: Sequence[Any],
    steps: Sequence[ReplayStep],
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
    zoom_channel_tag: str | None = None,
    zoom_target_value: Any = None,
    terminal_letrun_role_tags: tuple[str, ...] | None = None,
    replay_watch_roles: tuple[str, ...] = (),
    departure_bearing: tuple[tuple[str, Any], ...] = (),
    eject_cause_dones: frozenset[str] = frozenset(),
    progress_gauge: Any = None,
    progress_anchor: Mapping[str, Any] | None = None,
    eject_latch_baseline: tuple[tuple[str, Any], ...] = (),
) -> ReplayFn:
    """Build a replay callback for ``investigate_deviation``.

    The returned function forks from the checkpoint, installs existing holds
    plus the proposed hypothesis holds, and re-runs the act that surfaced the
    regression.

    The judgment depends on the incident shape:

    * **Channel incident** (``zoom_channel_tag`` set — a channel coast or a
      terminal let-run holding a macro-state) — a hold is *good* iff the
      channel register sits at its target/held value instead of ejecting.  The
      coast differs by shape: a **zoom** coast is unbounded and ejection-guarded
      (the immediate bearing may be a full coast away), a **let-run** coast is
      **bounded** to the departure window (its far-off global target is
      unreachable inside it).  In both cases the bearing's far-off conjuncts (the
      channel target, the global target, unrelated watch tags) are *not*
      required — only that the register did not eject — because a bounded coast
      cannot restore them and the bearing-held test would reject every hold.
    * **Terminal let-run without a channel register** — judge the global
      target at the bounded point.
    * **Command incident** — judge *departure_bearing* directly, else fall back
      to comparing the trace-back trend against the checkpoint trend.

    *New-cause progress* (``eject_cause_dones``): a channel/let-run hold that
    still ejects is normally rejected, but a one-sided liveness hold *fixes its
    own watchdog and trips the complement* — it must not be rejected for the
    complement's ejection, or round-by-round can never accumulate the second
    polarity.  So if the replay silenced an original ejecting watchdog Done bit
    and now ejects on a *different* accumulator Done, accept it as progress; the
    complement's ejection is the next round's incident.
    """
    all_done_tags = frozenset(
        owner.profile.done.name
        for owner in iter_advance_owners(program)
        if owner.profile.done is not None
    )

    def _replay(holds: tuple[Any, ...]) -> ReplayOutcome:
        from pyrung.core.analysis.pilot.coast import CoastSession

        probe = cp_fork.fork()
        probe_rungs = list(rungs)
        scope = _target_unresolved_condition(probe, target_tag, target_value)
        probe_rungs.extend(_rungs_from_proposals(probe, list(holds), scope))
        _set_rungs(probe, probe_rungs)
        # One session spans the whole replay; its pens on the profile Done bits
        # make the replay's own timeline the evidence ``_new_cause`` judges —
        # the same recorder the live incident used, never a history re-diff.
        session = CoastSession(probe, kind="replay")
        session.arm_pens(all_done_tags)
        # The last coast step IS the incident's eject coast; its receipt is the
        # bump-local verdict ("did the recorded departure reproduce?") the
        # judgment below reads alongside the endpoint snapshot.
        eject_receipt: Any = None
        for step in steps:
            # Each step re-arms its RECORDED session spec (``ReplayStep.kind``
            # off the committed step context) — a letrun eject-coast is coasted,
            # never pulsed (pulsing it would skip the coast entirely: five
            # settle scans, channel intact, every hypothesis "confirms").
            if step.kind == "pulse" and step.inputs:
                _apply_pulse(probe, list(step.inputs), resting, edge_tags, session=session)
            elif step.kind == "letrun":
                # The replay reproduces the INCIDENT — "the channel departed" —
                # so its watch roles (*replay_watch_roles*, an explicit caller
                # parameter) are the channel alone, never the live coast's full
                # role set: the checkpoint world catches the state machine's
                # scratch registers (isCmdValid__cmd, sm__where2jump)
                # mid-settlement, and a role guard would pause on that
                # transient with the channel still at its held value.  The
                # budget is the step's own recorded span — the replay seeks to
                # first-of {target, eject, timeout} and the judgment below
                # reads which fired, so no departure margin is needed.
                eject_receipt = _coast_holding_state(
                    probe,
                    target_tag,
                    target_value,
                    replay_watch_roles,
                    budget=max(1, step.scans),
                    session=session,
                )
            elif step.kind == "zoom" and step.channel_tag is not None:
                # Coast to the step's recorded bearing under the ejection
                # guard.  Do NOT bound this by the recorded span: the requested
                # value is the immediate goal but a full channel coast away,
                # and the guard already stops at the first ejection.
                eject_receipt = _coast_to_value(
                    probe, step.channel_tag, step.channel_target, session=session
                )
            else:
                session.dwell(max(1, step.scans))
        snap = dict(probe.state.tags)
        failure_silenced = bool(eject_latch_baseline) and all(
            _values_match(snap.get(tag), value) for tag, value in eject_latch_baseline
        )
        if (
            terminal_letrun_role_tags is not None
            and zoom_channel_tag is not None
            and _values_match(snap.get(zoom_channel_tag), zoom_target_value)
            and failure_silenced
        ):
            _observe_stable_channel_landing(
                probe,
                zoom_channel_tag,
                settle=not all(isinstance(hold, PilotRung) for hold in holds),
                session=session,
            )
            snap = dict(probe.state.tags)
        if logger.isEnabledFor(logging.DEBUG):
            roles = terminal_letrun_role_tags or ()
            logger.debug(
                "replay probe: cp_scan=%s end_scan=%s steps=%d shape=%s channel=%s=%r roles=%s",
                cp_fork.state.scan_id,
                probe.state.scan_id,
                len(steps),
                ("letrun" if terminal_letrun_role_tags is not None else "zoom"),
                zoom_channel_tag,
                snap.get(zoom_channel_tag) if zoom_channel_tag else None,
                {t: snap.get(t) for t in roles},
            )

        def _done_diff() -> tuple[frozenset[str], frozenset[str]]:
            """(silenced, new) Done-bit sets: incident window vs replay window.

            Symmetric evidence: the incident side (``eject_cause_dones``) is the
            Done bits that *fired inside the incident window*, so the replay side
            must be the Done bits that fired inside the *replay* window — read
            off the replay session's own timeline (its pens are the Done bits).
            Judging the replay by "Done bits true at the pause snapshot" compares
            unlike evidence: ambient always-true utility timers read as "new
            causes" and any window-only pulse reads as "silenced", so an
            irrelevant hold can score as progress.
            """
            replay_fired = frozenset(
                tag
                for event in session.events
                for tag, _before, _after in event.transitions
                if tag in all_done_tags
            )
            return (eject_cause_dones - replay_fired, replay_fired - eject_cause_dones)

        def _new_cause() -> str | None:
            """Reason string if this replay ejected on a *different* watchdog than
            the incident — the one-sided liveness hold fixed its own watchdog and
            tripped the complement — else ``None``."""
            if not eject_cause_dones:
                return None
            silenced, new = _done_diff()
            if silenced and new:
                return (
                    f"new-cause progress: silenced {sorted(silenced)}, now ejects on {sorted(new)}"
                )
            return None

        def _departure_silenced() -> str | None:
            """Reason string if the recorded departure did not reproduce.

            Bump-local judgment: the incident IS a departure bump; a hold is
            progress when the replay's eject coast reports the bump never fired
            (``stop_reason == "timeout"``, channel intact through the incident's
            own duration) — the machine now waits on the *next* blocker, which
            is the next round's incident.  A hold does not have to carry the
            channel all the way to the requested bearing to have solved this
            bump.  Corroboration: at least one of the incident's ejecting Done
            bits must be silenced on the replay's own timeline — a hold that
            freezes the channel pipeline leaves the alarm timers firing, so its
            silenced set is empty and it still rejects (the frozen-channel
            false confirm stays unrepresentable).
            """
            if eject_receipt is None or eject_receipt.stop_reason != "timeout":
                return None
            if not eject_cause_dones:
                return None
            silenced, _new = _done_diff()
            if silenced:
                return (
                    "departure bump silenced (coast timeout, channel intact): "
                    f"silenced {sorted(silenced)}"
                )
            return None

        # Channel incident (channel coast OR terminal let-run hold): the hold is
        # good iff the channel register sits at its target/held value instead of
        # ejecting — *reached* for a channel coast, *maintained* for a let-run
        # hold.  Either way the bearing's far-off conjuncts (the channel target
        # itself, the global target, unrelated watch tags) must NOT be required:
        # a bounded coast cannot restore them, so the bearing-held test would
        # reject every hold — including the latch-clears / liveness holds that
        # actually fix the ejection.  Ask the direct question against the
        # channel register instead.  The coast already differs by shape: the
        # zoom coast is unbounded and ejection-guarded (the requested value may
        # be a full coast away); the let-run coast is bounded to the departure window
        # (its global target is unreachable inside it).
        if zoom_channel_tag is not None:
            reached = _values_match(snap.get(zoom_channel_tag), zoom_target_value)
            progressed = _new_cause() if not reached else None
            if not reached and progressed is None:
                progressed = _departure_silenced()
            if (
                not reached
                and progressed is None
                and progress_gauge is not None
                and progress_anchor is not None
                and progress_gauge.compare(progress_anchor, snap) == "advanced"
            ):
                progressed = "target-relative progress advanced"
            if (
                not reached
                and progressed is None
                and eject_latch_baseline
                and all(_values_match(snap.get(tag), value) for tag, value in eject_latch_baseline)
            ):
                progressed = "observed latch failure silenced"
            return ReplayOutcome(
                accepted=reached or progressed is not None,
                trend=None,
                snapshot=snap,
                reason=progressed
                or (
                    f"{zoom_channel_tag} -> {zoom_target_value!r} reached={reached}"
                    + (
                        f" (eject coast: {eject_receipt.stop_reason})"
                        if eject_receipt is not None
                        else ""
                    )
                ),
                # A coast that timed out mid-journey landed nowhere — its end
                # snapshot must not seed a channel scope.
                landed=reached
                or (eject_receipt is not None and eject_receipt.stop_reason == "departed"),
            )

        # Terminal let-run without a channel register (no recognized state
        # machine): judge the global target at the bounded point.
        if terminal_letrun_role_tags is not None:
            reached = _values_match(snap.get(target_tag), target_value)
            progressed = _new_cause() if not reached else None
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
    # The retry pulse's recorded session events — the timeline the retry trial
    # carries forward (its Done-bit pen marks must stay visible to a later
    # incident window).
    retry_timeline: tuple[Any, ...] = ()


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
    rungs: Sequence[Any],
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
                chain = fork.cause(tag, deep=False)
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
    retry_rungs = list(rungs)
    from pyrung.core.analysis.pilot.coast import CoastSession
    from pyrung.core.condition import CompareEq

    preserved_tag = reverted[0]
    preserved = retry._known_tags_by_name[preserved_tag]
    scope = CompareEq(preserved, post_pulse_snap[preserved_tag])
    retry_rungs.extend(_rungs_from_proposals(retry, candidate_holds, scope))
    _set_rungs(retry, retry_rungs)
    kickoff = list(action)
    kickoff.extend((t, v) for t, v in candidate_holds if t not in {a for a, _ in action})
    session = CoastSession(retry, kind="excursion-retry")
    if program is not None:
        session.arm_pens(
            owner.profile.done.name
            for owner in iter_advance_owners(program)
            if owner.profile.done is not None
        )
    _apply_pulse(retry, kickoff, resting, edge_tags, session=session)
    _settle_delayed_effects(retry, pre_snap, cfg, scan_budget=scan_budget, session=session)
    retry_snap = dict(retry.state.tags)
    retry_key = _pilot_state_key(retry_snap, cfg)

    if retry_key != pre_key:
        return ExcursionResult(
            confirmed_holds=candidate_holds,
            reverted=reverted,
            retry_fork=retry,
            retry_timeline=session.events,
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
        chain = plc.cause(tag, deep=False)
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


def _first_timeline_departure(
    timeline: Sequence[Any],
    tag: str,
    value: Any,
) -> int | None:
    """The recorded scan of *tag*'s first transition off *value*, or ``None``.

    Read straight off the session timeline — the pen mark IS the departure
    scan; no history window is re-scanned.
    """
    for event in timeline:
        for t, before, after in event.transitions:
            if t == tag and _values_match(before, value) and not _values_match(after, value):
                return event.scan
    return None


def build_deviation_incident(
    *,
    anchor_scan: int,
    end_scan: int,
    action: tuple[ActionPair, ...],
    bearing: tuple[ActionPair, ...],
    before_snap: Mapping[str, Any],
    after_snap: Mapping[str, Any],
    timeline: Sequence[Any] = (),
    program: Any = None,
    channel_tag: str | None = None,
) -> DeviationIncident:
    """Capture the facts inside the known off-course window.

    *timeline* is the recorded session evidence for the window (the committed
    steps' pen marks and bump landings): ``changed_tags`` membership and every
    departure scan are read off it, never re-derived from history.  A
    fire-then-reset watchdog pulse is two recorded transitions — exactly the
    complement-reset oscillation ``correct_enablers`` looks for.

    ``changed_tags`` is factual incident evidence: every recorded transition
    plus every endpoint difference.  Consumers such as the timer correction
    engine select their own relevant profile tags from this complete set;
    incident construction never discards evidence on a consumer's behalf.

    *program* is retained for call compatibility.  It no longer changes the
    evidence recorded in the incident.
    """
    changed: set[str] = {t for event in timeline for t, _b, _a in event.transitions}
    changed.update(
        t
        for t in set(before_snap) | set(after_snap)
        if not _values_match(before_snap.get(t), after_snap.get(t))
    )
    departures = tuple(
        BearingDeparture(tag, value, _first_timeline_departure(timeline, tag, value))
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
        changed_tags=tuple(sorted(changed)),
        departures=departures,
        channel_tag=channel_tag,
        timeline=tuple(timeline),
    )


# ---------------------------------------------------------------------------
# Investigation engine
# ---------------------------------------------------------------------------


def _hold_is_noop(
    tag: str,
    value: Any,
    snap: Mapping[str, Any],
    pdg: Any,
    program: Any,
    incident_movers: frozenset[str] = frozenset(),
    after_snap: Mapping[str, Any] | None = None,
    synthesis_rungs: Sequence[PilotRung] = (),
) -> bool:
    """A hold that changes nothing cannot be a correction.

    Pinning *tag* at a value it already holds is inert when no program writer
    can move it off that value (every writer stamps a literal matching it —
    the clear-only idiom: holding ``Heat_xPause`` at its rest 0 counters
    nothing, because the program only ever writes 0).  A FREEZE survives this
    test: it either drives the tag OFF its current value or pins against a
    writer that can produce a different one.  Oscillating (``PilotRung``)
    values are never no-ops.

    A tag recorded as moving during this incident, whose endpoint differs from
    its anchor, or which is written by the installed synthesis overlay is not a
    no-op even when the proposed correction equals its anchor value.  The
    overlay is executable writer evidence outside the program PDG; replay, not
    this cheap prefilter, decides whether a different scoped rule is useful.
    """
    from pyrung.core.analysis.pdg import resolve_rung
    from pyrung.core.analysis.steerable import _literal_write

    if tag in incident_movers:
        return False
    if after_snap is not None and not _values_match(after_snap.get(tag), value):
        return False
    if getattr(value, "rules", None) is not None:
        return False
    if any(rung.dest == tag for rung in synthesis_rungs):
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
    primal_extra: frozenset[str] = frozenset(),
) -> list[InvestigationHypothesis]:
    """Order competing hypotheses by **causal primacy**, not generation order.

    The channel departure (``incident.channel_tag`` — the ejection itself)
    is the incident; other departures are collateral downstream of it (the
    state-8 shared-init resetting ``Heat_CurStep``).  Two primacy signals,
    strongest first:

    * **chain membership** — the hypothesis's tags sit inside the cause chain
      of the channel departure.  ``chase_chain_tags(..., bridge=ctx)`` crosses
      the opaque-pipeline hop by route inversion (the compass bridge): where the
      recorded-history walk dead-ends at a held ``S_StateRequested`` /
      ``isStateEnbl_Yes`` enabler, the bridge consults ``ctx.compass.graphs`` for
      the requesters of the observed destination transition, confirms which route
      fired against recorded history, and resumes the walk from that route's
      guard tags — so on a PackML-shaped program the chain reaches the starved
      watchdog directly instead of stopping short of it.
    * **temporal precedence** — how close the hypothesis's most recent source
      transition sits to the channel departure scan.  Pure scan-log
      observation, no inversion: the ejecting watchdog's Done rises *at* the
      ejection; a bystander (``Test_Simulate_1st_Scan``'s alarm timer) fired
      somewhere earlier in a 1000-scan coast, and a collateral symptom
      (``Heat_CurStep`` at 1810 vs the ejection at 1855) trails by the same
      measure.

    Ties break by lightest intervention, then generation order.
    """
    chan = incident.channel_tag
    dep_scan = {d.tag: d.scan for d in incident.departures if d.scan is not None}
    primal: set[str] = set()
    chan_scan = incident.end_scan
    if chan is not None:
        if dep_scan.get(chan) is not None:
            chan_scan = dep_scan[chan]
        # All tags on the chain, not just steerable roots: an absence-caused
        # ejection (a sensor that never moved) has no steerable mover at all.
        # ``bridge=ctx`` crosses the opaque-pipeline hop by route inversion, so
        # the chain reaches the true root (the starved watchdog) instead of
        # dead-ending at the held ``S_StateRequested`` enabler — making causal
        # primacy exact rather than won on temporal proximity.
        primal = {chan} | chase_chain_tags(plc, chan, scan=dep_scan.get(chan), bridge=ctx)
    # Deep-walk roots of the channel departure (``primal_extra``) are chain
    # members by construction — an absence root has no transition for the
    # proximity signal to see, so without this it would rank dead last behind
    # every temporally-nearby bystander.
    primal |= primal_extra

    big = 1 << 30

    def _proximity(tags: set[str]) -> int:
        best = big
        for t in tags:
            last = _last_transition_scan(plc, t, incident.anchor_scan, chan_scan)
            if last is not None:
                best = min(best, chan_scan - last)
        return best

    def _key(pair: tuple[int, InvestigationHypothesis]) -> tuple[int, int, int, int]:
        idx, h = pair
        tags = set(h.sources) | {_proposal_pair(p)[0] for p in h.holds}
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
    installed_rungs: Sequence[Any] = (),
    correction_progress_mark: tuple[tuple[str, Any], ...] = (),
) -> InvestigationResult:
    """Investigate an incident with precise hypothesis generation.

    Two sources, both instrument-derived:
    1. Precise cause walk — single cause()-chain from the first departure
       that reaches a steerable input (the *trigger-found* case).
    2. Enabler correction — when cause finds no steerable trigger, the held
       enablers are the cause; ``correct_enablers`` dispatches by writer
       instruction (coil latch -> FLIP guard, accumulator -> OSCILLATE /
       stop-hold).
    No upstream cone sweep.

    Hypotheses are **competing explanations of one incident, not a bundle of
    independent fixes**: they are ranked by causal primacy
    (:func:`_rank_hypotheses`) and the FIRST hypothesis that survives the
    static self-defeat check (*needed* — the checkpoint frontier) and the
    replay is confirmed **alone**. A union of individually-replayed holds is an
    untested configuration — installing exactly one keeps the installed set
    exactly what was replayed. Hypotheses whose holds are *already installed*
    (*installed*) are skipped, not re-confirmed: they were active when the
    incident happened, so a repeat regression at the same key escalates to the
    runner-up instead of re-anointing the incumbent.
    """
    # Absence roots generate FIRST: rank ties inside the causal chain break by
    # generation order, and when a never-moved terminal (the stuck permissive)
    # and a mid-chain suppressor (the abort rung's ~Suspend enabler) both
    # survive the bounded replay, the terminal names the cause while the
    # suppressor merely mutes the response.
    installed_rungs = tuple(installed_rungs)
    # The effective pilot-held value per dest *at the incident anchor*: managed
    # lowering returns each Boolean to its rest, then active rungs write in append
    # order, so the last active rung wins.  A rung whose guard evaluated False at
    # the anchor (an expired door hold in Execute) contributes nothing — it was
    # NOT active when the incident happened and must not gate the installed-skip.
    _before_view = _SnapshotView(dict(incident.before_snap), {})
    installed_active: dict[str, Any] = {}
    for _rung in installed_rungs:
        if bool(_rung.guard.evaluate(_before_view)):
            installed_active[_rung.dest] = _rung.value
    absence_hyps, absence_tags = _absence_root_correctives(
        plc,
        incident,
        ctx,
        # Protect only the action that launched this incident. Historical
        # Pilot ownership is not causal evidence; deep cause replays the actual
        # installed synthesis and can attribute an active or expired rule.
        exclude=frozenset(tag for tag, _value in incident.action),
    )
    raw: list[InvestigationHypothesis] = list(absence_hyps)
    raw.extend(_precise_causes(plc, incident, ctx))
    raw.extend(
        InvestigationHypothesis(kind=c.kind, holds=c.holds, sources=c.sources, detail=c.detail)
        for c in correct_enablers(plc, incident, ctx)
    )
    hypotheses = _rank_hypotheses(
        plc, _dedupe_hypotheses(raw), incident, ctx, primal_extra=absence_tags
    )
    confirmed: list[InvestigationHypothesis] = []
    rejected: list[tuple[InvestigationHypothesis, str]] = []
    rejection_slugs: list[str] = []
    confirmed_holds: list[Any] = []
    pdg = getattr(ctx, "pdg", None)
    program = getattr(ctx, "program", None)
    # A proposed hold at the anchor value is meaningful when the complete
    # incident record says that tag moved away (including an installed guard
    # expiring).  Correction engines filter this factual set locally.
    recorded_incident_movers = frozenset(incident.changed_tags)

    def _reject(hyp: InvestigationHypothesis, slug: str, detail: str) -> None:
        # Recording only: ``detail`` is the unchanged human ground, ``slug`` the
        # index-aligned machine-readable classification.  Appending through one
        # helper keeps ``rejected`` and ``rejection_slugs`` in lock-step.
        rejected.append((hyp, detail))
        rejection_slugs.append(slug)

    for hypothesis in hypotheses:
        if not hypothesis.holds:
            _reject(hypothesis, "no-holds", "no holds proposed")
            continue
        if installed_active and all(
            ht in installed_active
            and (installed_active[ht] == hv or _values_match(installed_active[ht], hv))
            for ht, hv in map(_proposal_pair, hypothesis.holds)
        ):
            # Skip only when an installed rung *actively covered* every proposed
            # pair at the incident anchor: it was truly active when the incident
            # happened, so a repeat regression escalates to the runner-up.  A rung
            # installed but guard-expired (a door hold released in Execute) is
            # absent here, so its hypothesis proceeds to replay instead.
            continue
        if (
            pdg is not None
            and program is not None
            and all(
                not any(
                    action_tag == ht and not _values_match(action_value, hv)
                    for action_tag, action_value in incident.action
                )
                and _hold_is_noop(
                    ht,
                    hv,
                    incident.before_snap,
                    pdg,
                    program,
                    recorded_incident_movers,
                    incident.after_snap,
                    installed_rungs,
                )
                for ht, hv in map(_proposal_pair, hypothesis.holds)
            )
        ):
            # Every hold pins a value already in place that the program cannot
            # move — the "correction" changes nothing, so its replay pass is
            # vacuous and installing it burns the round on a byte-identical
            # re-coast.
            _reject(
                hypothesis,
                "vacuous-hold",
                "vacuous no-op hold: every proposed value is already stable in the incident anchor",
            )
            continue
        outcome = replay(hypothesis.holds)
        if outcome.accepted:
            scoped = _scoped_correction_rungs(
                plc,
                hypothesis.holds,
                incident,
                outcome,
                ctx,
                correction_progress_mark,
            )
            required_progress = (*incident.bearing, *needed)
            if (
                pdg is not None
                and program is not None
                and _active_rungs_defeat_needed(
                    scoped,
                    required_progress,
                    incident.before_snap,
                    pdg,
                    program,
                )
            ):
                # Replay windows are deliberately bounded to the incident. A
                # correction can silence that incident yet pin a slower progress
                # register behind the checkpoint frontier after the window ends.
                # Screen the exact guarded form that would be installed; the
                # guard limits where the pin applies, but cannot make it harmless
                # while that context is active.
                _reject(
                    hypothesis,
                    "self-defeat",
                    "guarded correction defeats requested progress: "
                    f"needed={required_progress!r}, correction={tuple(scoped)!r}",
                )
                continue
            installed_outcome = replay(scoped)
            if installed_outcome.accepted:
                confirmed_hypothesis = InvestigationHypothesis(
                    kind=hypothesis.kind,
                    holds=scoped,
                    sources=hypothesis.sources,
                    detail=hypothesis.detail,
                )
                confirmed.append(confirmed_hypothesis)
                confirmed_holds.extend(scoped)
                break  # first confirmed wins — one intervention per incident
            _reject(
                hypothesis,
                "guarded-replay-failed",
                "guarded replay rejected: "
                + (installed_outcome.reason or "no replay reason supplied"),
            )
            continue
        _reject(
            hypothesis,
            "exploratory-replay-failed",
            "raw replay rejected: " + (outcome.reason or "no replay reason supplied"),
        )

    return InvestigationResult(
        confirmed_holds=tuple(_dedupe_pairs(confirmed_holds)),
        regression_nogoods=frozenset(),
        hypotheses=tuple(hypotheses),
        confirmed=tuple(confirmed),
        rejected=tuple(rejected),
        rejection_slugs=tuple(rejection_slugs),
        unresolved=incident.changed_tags if not confirmed else (),
    )


# ---------------------------------------------------------------------------
# Hypothesis generation — precise pass
# ---------------------------------------------------------------------------


def _hold_values(hold_value: Any) -> tuple[Any, ...]:
    """The steady values a hold can pin its tag to: a scalar hold is that value;
    a ``PilotRung`` contributes each of its rule target values (an
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

    Direct contradictions are harmful too: a correction that actively writes a
    required tag to another value is already a proof of self-defeat. The
    writer walk below additionally catches an indirect pin where the held value
    forces some other required register away from its need.
    """
    return _holds_defeat_needed(((tag, hold_value),), needed, pdg, program)


def _active_rungs_defeat_needed(
    rungs: Sequence[PilotRung],
    needed: Sequence[tuple[str, Any]],
    snapshot: Mapping[str, Any],
    pdg: Any,
    program: Any,
) -> bool:
    """Whether the guarded correction provably pins a checkpoint need.

    Guards are evaluated in the exact pre-incident world because synthesized
    PilotRung branches read one frozen rung-entry snapshot. Inactive or
    unevaluable guards cannot prove a rejection. Active rungs are checked as
    one assignment, so a coordinated correction that forces an ``And``-gated
    reset is caught even when no member defeats progress alone.
    """
    view = _SnapshotView(dict(snapshot), {})
    active: list[tuple[str, Any]] = []
    for rung in rungs:
        try:
            if bool(rung.guard.evaluate(view)):
                active.append((rung.dest, rung.value))
        except (AttributeError, KeyError, TypeError, ValueError):
            continue
    return _holds_defeat_needed(active, needed, pdg, program)


def _holds_defeat_needed(
    holds: Sequence[tuple[str, Any]],
    needed: Sequence[tuple[str, Any]],
    pdg: Any,
    program: Any,
) -> bool:
    """Static write-vs-need proof for one executable hold assignment."""
    from pyrung.core.analysis.pdg import resolve_rung
    from pyrung.core.analysis.simplified import Atom, _conditions_list_to_expr, _expr_forced_true
    from pyrung.core.analysis.steerable import _literal_write

    needed_first: dict[str, Any] = {}
    for nt, nv in needed:
        if isinstance(nv, Atom):
            # A relational need (``PV < Lower``) carries its Atom, not a value —
            # this static write-vs-need check can't reason about relations, so
            # it honestly punts on that entry (never treats the Atom as a value).
            continue
        needed_first.setdefault(nt, nv)
    if not needed_first:
        return False
    held_values: dict[str, tuple[Any, ...]] = {}
    for tag, hold_value in holds:
        held_values[tag] = _hold_values(hold_value)
    if not held_values:
        return False
    if any(
        tag in needed_first and any(not _values_match(value, needed_first[tag]) for value in values)
        for tag, values in held_values.items()
    ):
        return True
    for node in pdg.rung_nodes:
        read_tags = tuple(tag for tag in node.condition_reads if tag in held_values)
        if not read_tags:
            continue
        ro = resolve_rung(program, node)
        if ro is None:
            continue
        expr = _conditions_list_to_expr(getattr(ro, "_conditions", []))
        assignments = (
            dict(zip(read_tags, values, strict=True))
            for values in product(*(held_values[tag] for tag in read_tags))
        )
        if not any(_expr_forced_true(expr, assignment) is True for assignment in assignments):
            continue
        for nt, first_need in needed_first.items():
            wv = _literal_write(ro, nt)
            if wv is not None and not _values_match(wv, first_need):
                return True
    return False


def _precise_causes(
    plc: PLC,
    incident: DeviationIncident,
    ctx: Any,
) -> list[InvestigationHypothesis]:
    """Minimal controllable cuts of the exact deep fired chain.

    For each departure, ``cause(deep=True)`` supplies the rungs that actually
    fired, their exact transitions, and their steady enablers. The walk derives
    two forms of cut from that one record:

    * revert a steerable transition at its pre-incident value;
    * force a fired rung's guard false with the cheapest drivable assignment.

    Program-written condition tags are never terminal levers merely because
    static steerability includes them; guard solving follows their
    observed/static writers to an external lever. Cuts that oppose requested
    progress are still hypotheses: the investigation layer proves them harmful
    against the incident bearing/checkpoint frontier and records a
    ``self-defeat`` rejection. Every returned hypothesis names the fired rung
    whose conductive path it cuts.
    """
    steerable = getattr(ctx, "steerable", frozenset())
    if not steerable:
        return []
    pdg = getattr(ctx, "pdg", None)
    program = getattr(ctx, "program", None)
    if pdg is None or program is None:
        return []
    empirical_writes = empirical_program_writes(
        plc,
        steerable,
        start_scan=incident.anchor_scan,
        end_scan=incident.end_scan,
    )
    hypotheses: list[InvestigationHypothesis] = []

    # The channel departure is the incident's causal effect. Bearing aliases
    # (``Sts_State_Starting`` falling because the channel already left Starting)
    # are downstream symptoms and must not seed cuts of their observer/mapping
    # rungs. Coast receipts retain the exact channel transition even when the
    # requested destination was never reached and therefore has no ordinary
    # ``BearingDeparture.scan``.
    seeds = list(incident.departures)
    if incident.channel_tag is not None:
        channel_scan = next(
            (
                event.scan
                for event in reversed(incident.timeline)
                if any(
                    tag == incident.channel_tag and not _values_match(before, after)
                    for tag, before, after in getattr(event, "transitions", ())
                )
            ),
            None,
        )
        if channel_scan is not None:
            desired = next(
                (value for tag, value in incident.bearing if tag == incident.channel_tag),
                incident.before_snap.get(incident.channel_tag),
            )
            seeds = [BearingDeparture(incident.channel_tag, desired, channel_scan)]

    for departure in seeds:
        try:
            chain = plc.cause(departure.tag, scan=departure.scan, deep=True)
        except Exception:  # noqa: BLE001
            logger.debug(
                "causal-frontier: cause(%s@%s) raised",
                departure.tag,
                departure.scan,
                exc_info=True,
            )
            chain = None
        if chain is None:
            continue

        steps_by_tag: dict[str, list[Any]] = {}
        for step in chain.steps:
            steps_by_tag.setdefault(step.transition.tag_name, []).append(step)

        # Static steerability is narrowed only by exact evidence that the user
        # program/plant authored the transition.  A recorded synthesis writer
        # does not by itself turn its external destination into an internal
        # intermediate; the causal walk may still terminate at that lever.
        effective_steerable = frozenset(steerable) - empirical_writes

        origin_memo: dict[str, frozenset[str]] = {}

        def _origins(
            name: str,
            visiting: frozenset[str] = frozenset(),
            *,
            _steps_by_tag: dict[str, list[Any]] = steps_by_tag,
            _origin_memo: dict[str, frozenset[str]] = origin_memo,
        ) -> frozenset[str]:
            if name in _origin_memo:
                return _origin_memo[name]
            if name in visiting:
                return frozenset()
            next_visiting = visiting | {name}
            found: set[str] = set()
            for step in _steps_by_tag.get(name, ()):
                links = step.triggers or step.enablers
                for link in links:
                    found.update(_origins(link.tag_name, next_visiting))
            result = frozenset(found or {name})
            _origin_memo[name] = result
            return result

        def _step_label(step: Any) -> str:
            return f"{step.subroutine + ':' if step.subroutine else ''}R{step.rung_index + 1}"

        # The undesired path is the trigger spine from the effect. Deep enabler
        # expansion supplies origins for conditions on that spine, but those
        # supporting writer rungs are not themselves antagonists to cut.
        trigger_spine: set[int] = set()

        def _mark_trigger_spine(
            transition: Any,
            visiting: frozenset[tuple[str, int]] = frozenset(),
            *,
            _steps_by_tag: dict[str, list[Any]] = steps_by_tag,
            _trigger_spine: set[int] = trigger_spine,
        ) -> None:
            key = (transition.tag_name, transition.scan_id)
            if key in visiting:
                return
            next_visiting = visiting | {key}
            for step in _steps_by_tag.get(transition.tag_name, ()):
                if step.transition.scan_id != transition.scan_id or not _values_match(
                    step.transition.to_value,
                    transition.to_value,
                ):
                    continue
                _trigger_spine.add(id(step))
                for trigger in step.triggers:
                    _mark_trigger_spine(trigger, next_visiting)

        _mark_trigger_spine(chain.effect)

        # First candidate: exact transitioned leaves. This is the same causal
        # frontier as the rung cuts below, not a separate heuristic; it is
        # preferred because preserving the pre-transition physical value is the
        # lightest faithful correction.
        nogoods, mover_holds = chase_cause_roots(
            plc,
            departure.tag,
            effective_steerable,
            scan=departure.scan,
            bridge=ctx,
        )
        moved_tags = {
            tr.tag_name
            for step in chain.steps
            if id(step) in trigger_spine
            for tr in step.triggers
            if not _values_match(tr.from_value, tr.to_value)
        }
        mover_holds_filtered = tuple(
            pair
            for pair in _dedupe_pairs(mover_holds)
            if pair[0] in moved_tags and _hold_allowed(ctx, pair)
        )
        if mover_holds_filtered:
            mover_names = {tag for tag, _value in mover_holds_filtered}
            common: list[tuple[int, Any]] = []
            for index, step in enumerate(chain.steps):
                if id(step) not in trigger_spine:
                    continue
                leaves: set[str] = set()
                for trigger in step.triggers:
                    leaves.update(_origins(trigger.tag_name))
                if mover_names <= leaves:
                    common.append((index, step))
            frontier = common[-1][1] if common else chain.steps[0]
            hypotheses.append(
                InvestigationHypothesis(
                    kind="precise-cause",
                    holds=mover_holds_filtered,
                    sources=tuple(sorted(nogoods | mover_names | {departure.tag})),
                    detail=(
                        f"{_step_label(frontier)} fired at scan "
                        f"{frontier.transition.scan_id}; revert exact trigger frontier"
                    ),
                )
            )

        if departure.scan is None:
            frame = dict(plc.state.tags)
        else:
            try:
                frame = dict(plc.history.at(departure.scan).tags)
            except Exception:  # noqa: BLE001
                frame = dict(plc.state.tags)

        # Then enumerate minimal guard cuts for every actual fired rung. Reads
        # are all eligible hypotheses, including cuts through requested
        # progress. The investigation layer, not the generator, records why a
        # progress-damaging cut is harmful.
        from pyrung.core.analysis.pdg import resolve_rung

        for step in reversed(chain.steps):
            if id(step) not in trigger_spine:
                continue
            direct_values = {
                **{tr.tag_name: tr.to_value for tr in step.triggers},
                **{ec.tag_name: ec.value for ec in step.enablers},
            }
            if not direct_values:
                continue
            node = next(
                (
                    pdg.rung_nodes[node_idx]
                    for node_idx in sorted(
                        pdg.writers_of.get(step.transition.tag_name, frozenset())
                    )
                    if (
                        pdg.rung_nodes[node_idx].rung_index,
                        pdg.rung_nodes[node_idx].subroutine,
                    )
                    == (step.rung_index, step.subroutine)
                ),
                None,
            )
            if node is None:
                continue
            rung_obj = resolve_rung(program, node)
            if rung_obj is None:
                continue
            writer = _writer_for_tag(rung_obj, step.transition.tag_name)
            if writer is None:
                continue
            if not getattr(writer, "INERT_WHEN_DISABLED", True):
                # A false rung is not necessarily a suppressed writer. OUT
                # actively writes False when disabled; timers/counters and
                # drums also have instruction-specific disabled behavior.
                # Only the ordinary OUT-to-True case has the generic
                # "make guard false" inverse. Other non-inert writers belong
                # to their instruction-specific correction machinery.
                from pyrung.core.instruction.coils import OutInstruction

                if not (isinstance(writer, OutInstruction) and step.transition.to_value is True):
                    continue
            guard_reads = set(getattr(node, "condition_reads", ())) & set(direct_values)
            fixed: dict[str, Any] = {}
            changeable = guard_reads
            if not changeable:
                continue
            fire_frame = {**frame, **direct_values}
            holds = break_guard_holds(
                rung_obj,
                fire_frame,
                ctx,
                changeable=changeable,
                fixed=fixed,
                steerable=effective_steerable,
            )
            holds_filtered = tuple(
                pair for pair in _dedupe_pairs(holds or ()) if _hold_allowed(ctx, pair)
            )
            if not holds_filtered:
                continue
            hypotheses.append(
                InvestigationHypothesis(
                    kind="precise-cause",
                    holds=holds_filtered,
                    sources=tuple(
                        sorted(
                            {
                                departure.tag,
                                step.transition.tag_name,
                                *direct_values,
                                *(tag for tag, _value in holds_filtered),
                            }
                        )
                    ),
                    detail=(
                        f"{_step_label(step)} fired at scan "
                        f"{step.transition.scan_id}; minimal conductive cut"
                    ),
                )
            )
    return list(_dedupe_hypotheses(hypotheses))


def _precise_cause(
    plc: PLC,
    incident: DeviationIncident,
    ctx: Any,
) -> InvestigationHypothesis | None:
    """Compatibility helper returning the first exact causal frontier."""
    hypotheses = _precise_causes(plc, incident, ctx)
    return hypotheses[0] if hypotheses else None


# ---------------------------------------------------------------------------
# Hypothesis generation — absence roots (deep cause walk)
# ---------------------------------------------------------------------------

_ABSENCE_ROOT_KINDS = frozenset({"external", "never_written"})

#: logical negation of an ordered-comparison form — the analog "flip".
_NEGATE_FORM = {"lt": "ge", "le": "gt", "gt": "le", "ge": "lt"}


def _ordered_truth(form: str, lhs: Any, rhs: Any) -> bool | None:
    """Truth of ``lhs <form> rhs``, or ``None`` when the pair doesn't order."""
    try:
        return {
            "lt": lhs < rhs,
            "le": lhs <= rhs,
            "gt": lhs > rhs,
            "ge": lhs >= rhs,
        }[form]
    except TypeError:
        return None


def _analog_boundary_hold(
    plc: PLC,
    root: Any,
    chain: Any,
    ctx: Any,
) -> tuple[tuple[str, Any], str] | None:
    """The analog analogue of the Bool flip: ``(hold, note)`` for a wide root.

    A Bool absence root flips to its complement; a wide word has none — but
    the chain knows what the stuck value *does*: the root supports the fault
    path through an ordered comparison on one of the chain's rungs.  So flip
    the comparison's truth instead: solve the boundary of the flipped atom
    against the current snapshot (the same stage-2/stage-3 resolvers as the
    trace's relational levers) and propose that value as the corrective hold.
    A guess is fine because it is replay-verified; a root with no ordered
    comparison on the chain's rungs still yields nothing (fail closed), and
    the comparison search never leaves the recorded chain — a program-wide
    sweep would invent levers from rungs that played no part in the incident.
    """
    from pyrung.core.analysis.pdg import resolve_rung
    from pyrung.core.analysis.pilot.trace import (
        _atom_text,
        _heuristic_inequality_target,
        _resolve_inequality_target,
    )
    from pyrung.core.analysis.simplified import And, Atom, Or, _conditions_list_to_expr
    from pyrung.core.analysis.sp_values import _FLIP_FORM

    name = root.tag_name
    pdg = getattr(ctx, "pdg", None)
    program = getattr(ctx, "program", None)
    if pdg is None or program is None:
        return None
    snapshot = dict(plc.state.tags)
    steerable = getattr(ctx, "steerable", frozenset())
    prior = getattr(ctx, "domain_prior", None)

    def _iter_atoms(expr: Any) -> Any:
        if isinstance(expr, Atom):
            yield expr
        elif isinstance(expr, (And, Or)):
            for term in expr.terms:
                yield from _iter_atoms(term)

    step_keys = {(s.rung_index, s.subroutine) for s in chain.steps}
    seen: set[tuple[str, str, Any]] = set()
    for node in pdg.rung_nodes:
        if name not in getattr(node, "condition_reads", ()):
            continue
        if (node.rung_index, node.subroutine) not in step_keys:
            continue
        rung = resolve_rung(program, node)
        if rung is None:
            continue
        for atom in _iter_atoms(_conditions_list_to_expr(getattr(rung, "_conditions", []))):
            # Key the atom on the root (operand side flips via A>B ⟺ B<A).
            if atom.tag == name:
                atom_on_root = atom
            elif atom.operand == name and atom.form in _FLIP_FORM:
                atom_on_root = Atom(tag=name, form=_FLIP_FORM[atom.form], operand=atom.tag)
            else:
                continue
            if atom_on_root.form not in _NEGATE_FORM or atom_on_root._key() in seen:
                continue
            seen.add(atom_on_root._key())
            operand = atom_on_root.operand
            threshold = snapshot.get(operand) if isinstance(operand, str) else operand
            truth = _ordered_truth(atom_on_root.form, root.value, threshold)
            if truth is None:
                continue
            # Cross the boundary AWAY from the value's current contribution:
            # satisfy the negation of whatever the stuck value makes true.
            goal = (
                Atom(tag=name, form=_NEGATE_FORM[atom_on_root.form], operand=operand)
                if truth
                else atom_on_root
            )
            target = _resolve_inequality_target(goal, snapshot, prior, pdg)
            marker = ""
            if target is None or target[0] != name:
                hit = _heuristic_inequality_target(goal, snapshot, steerable, pdg)
                if hit is None:
                    continue
                value, marker = hit
                target = (name, value)
            tag, value = target
            if _values_match(snapshot.get(tag), value):
                continue  # not a move
            note = f"cross {_atom_text(goal)} (e.g., {tag} = {value!r}"
            if marker:
                note += f"; {marker}"
            note += ")"
            return (tag, value), note
    return None


def _absence_root_correctives(
    plc: PLC,
    incident: DeviationIncident,
    ctx: Any,
    exclude: frozenset[str] = frozenset(),
) -> tuple[list[InvestigationHypothesis], frozenset[str]]:
    """Corrective holds from the deep walk's never-moved roots.

    The shallow chase cannot reach a cause that never transitioned — a
    permissive held open since cold, buffered behind an intermediate error
    register and laundered through a block sum (the sail trap).  The deep
    recorded walk (``cause(deep=True)``) names exactly those terminals:
    ``RootCause`` entries with ``held_since_scan=None``.  Each steerable,
    never-moved Bool root becomes a FLIP hold hypothesis, replay-tested
    like any other — a guess is fine because it is replay-verified.

    Returns the hypotheses plus the root tag names, which the caller feeds
    to ``_rank_hypotheses`` as ``primal_extra``: an absence root produces no
    transition for the temporal-proximity signal, so without chain-member
    standing it would rank behind every temporally-nearby bystander whose
    hold merely defers the fault past the bounded replay window.

    *exclude* carries the pilot-touched and installed-hold tags: a tag the
    pilot itself pinned reads as "held since cold" in the fork's history,
    but flipping the pilot's own hold is self-investigation, not a program
    absence.
    """
    chan = incident.channel_tag
    dep = None
    if chan is not None:
        dep = next((d for d in incident.departures if d.tag == chan), None)
    if dep is None:
        dep = next(iter(incident.departures), None)
    if dep is None:
        return [], frozenset()
    try:
        chain = plc.cause(dep.tag, scan=dep.scan)
    except Exception:  # noqa: BLE001
        logger.debug("absence-root: cause(%s) raised", dep.tag, exc_info=True)
        return [], frozenset()
    if chain is None:
        return [], frozenset()

    steerable = getattr(ctx, "steerable", frozenset())
    if logger.isEnabledFor(logging.DEBUG):
        logger.debug(
            "absence-root: %s@%s roots=%s",
            dep.tag,
            dep.scan,
            [(r.tag_name, r.value, r.kind, r.held_since_scan) for r in chain.ranked_roots()],
        )
    keyed: list[tuple[int, InvestigationHypothesis]] = []
    root_tags: set[str] = set()
    for root in chain.ranked_roots():
        if root.kind not in _ABSENCE_ROOT_KINDS:
            continue
        if root.held_since_scan is not None:
            continue  # it moved during the run — not an absence
        if root.tag_name in exclude:
            continue  # the pilot's own hold, not a program absence
        if root.tag_name not in steerable:
            continue
        if isinstance(root.value, bool):
            hold = (root.tag_name, not root.value)
            relation_note = ""
        else:
            # A wide word offers no complement, but the chain knows what the
            # stuck value does — flip the truth of the ordered comparison it
            # supports and propose the boundary value (replay-verified).
            analog = _analog_boundary_hold(plc, root, chain, ctx)
            if analog is None:
                continue  # no ordered comparison on the chain — still no sound value
            hold, note = analog
            relation_note = f"; {note}"
        if not _hold_allowed(ctx, hold):
            continue
        root_tags.add(root.tag_name)
        keyed.append(
            (
                len(root.via),
                InvestigationHypothesis(
                    kind="absence-root",
                    holds=(hold,),
                    sources=(root.tag_name,),
                    detail=(
                        f"{root.tag_name} held {root.value!r} since cold "
                        f"on {dep.tag}'s deep cause chain [{root.kind}]{relation_note}"
                    ),
                ),
            )
        )
    # Deepest terminal first: the fault-generation side (a permissive buffered
    # behind an error register and an alarm chain) sits deeper in the chain
    # than a response-side gate on the abort rung itself, and the bounded
    # replay cannot distinguish them — both keep the channel in place.  The
    # hop-provenance length is the depth proxy; ties keep ranked_roots order.
    keyed.sort(key=lambda kv: -kv[0])
    return [h for _, h in keyed], frozenset(root_tags)


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


def _last_transition_scan(
    plc: PLC,
    tag: str,
    start_scan: int,
    end_scan: int,
) -> int | None:
    """The latest scan in the window where *tag* changed value, or ``None``.

    The temporal-precedence signal for hypothesis ranking: the watchdog Done
    that ejected the bearing rises *at* the channel departure; a bystander
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


def _dedupe_pairs(pairs: Iterable[ActionPair]) -> list[ActionPair]:
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
