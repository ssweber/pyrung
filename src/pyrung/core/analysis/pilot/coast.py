"""Run trigger-driven coasts and return exact observation receipts.

A ``CoastTrigger`` is a named state predicate. ``CoastSession`` advances with folding
when safe, lands each crossing on a real recorded scan, re-arms nonterminal
triggers, and records simultaneous terminal triggers in a ``CoastReceipt``.
Steering execution may also arm trial-start-clear avoid triggers: readable
conditions constrain folded logical spans, while opaque predicates observe only
the real kernel scans a fold executes.

The predicate callable decides whether a trigger fired. An optional compiled
condition supplies crossing and protected-read metadata for folding only; it
does not replace predicate semantics. This module records what happened but
does not classify the observation as progress, regression, or acceptance.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from pyrung.core.analysis.sp_values import _values_match

if TYPE_CHECKING:
    from pyrung.core.runner import PLC

logger = logging.getLogger(__name__)

# Coast-trigger kinds are strings, not an enum — the vocabulary grows per cutover phase
# and consumers match on names they know.
TARGET = "target"
DEPARTURE = "departure"
QUIESCENT = "quiescent"
PEN = "pen"
AVOID = "avoid"


@dataclass(frozen=True)
class CoastLimits:
    """The named policy horizons, centralized.  Values only — the *decision*
    of when each applies stays with its owner (one owner per decision)."""

    cone_floor: int = 2  # ladder logic takes ≤2 scans to propagate
    cone_ceiling: int = 16
    dwell_ceiling: int = 64
    pulse_settle_scans: int = 4
    # A departure's own transition chain (Holding -> Held) completes in
    # scans-to-seconds; the channel must stay silent for a full second of
    # scans before the landing is trusted.  A *policy* window, not a
    # stepping loop — the confirmation seek folds through the silence.
    landing_confirm_scans: int = 100
    landing_cap: int = 2_000


LIMITS = CoastLimits()

# A bearing coast gets a generous budget of its own — timer dwell is waiting,
# not searching, so it does not consume the pilot's iteration budget.
_COAST_BUDGET = 10_000


@dataclass(frozen=True)
class CoastTrigger:
    """One armed pen on the trend recorder.

    ``predicate`` is authoritative.  ``condition`` (a compiled ``Condition``)
    is fold metadata only: its comparison atoms become crossing targets and
    its reads become fold-protected tags.  ``watched`` names the tags whose
    transitions the receipt records when this trigger fires. ``terminal`` triggers
    end the seek; nonterminal triggers record an event and re-arm (``one_shot``
    disarms after the first firing instead).
    """

    name: str
    kind: str
    predicate: Callable[[Any], bool]
    condition: Any | None = None
    watched: tuple[str, ...] = ()
    terminal: bool = True
    one_shot: bool = False


@dataclass(frozen=True)
class CoastTriggerEvent:
    """One pen mark: a coast trigger firing at an exact scan."""

    name: str
    kind: str
    scan: int
    transitions: tuple[tuple[str, Any, Any], ...]  # (tag, before, after)


@dataclass(frozen=True)
class CoastReceipt:
    """What one seek observed.  Values only — safe to carry across reverts.

    ``stop_reason``: ``"reached"`` (a target trigger fired), ``"departed"`` (a
    departure fired without a target), ``"quiescent"`` (a quiescence trigger or
    cone fixpoint), ``"timeout"`` (budget/ceiling exhausted, nothing fired),
    ``"paused"`` (an external pause stopped the coast early), ``"dwell"``
    (a fixed dwell completed), or ``"skipped"`` (nothing to coast).
    ``fired`` names every terminal trigger true at the landing scan —
    simultaneous firings are all present.  ``trajectory`` is populated only
    by :meth:`CoastSession.settle` (per-scan snapshots of the dwell).
    """

    kind: str
    start_scan: int
    end_scan: int
    stop_reason: str
    fired: tuple[str, ...]
    events: tuple[CoastTriggerEvent, ...]
    budget: int
    kernel_scans: int = 0
    macro_folds: int = 0
    trajectory: tuple[dict[str, Any], ...] = ()
    # Exact accumulator destinations written by cycle folding, in execution
    # order. These are the manual edits needed to reproduce each jump ahead.
    advances: tuple[tuple[str, Any], ...] = ()
    # Cheap scalar timer carry updates replayed in lieu of full kernel scans.
    timer_quanta_replayed: int = 0

    @property
    def logical_scans(self) -> int:
        """Logical scan IDs advanced, including scans skipped by folds."""
        return self.end_scan - self.start_scan

    @property
    def skipped_scans(self) -> int:
        """Logical scans advanced without an interpreter execution."""
        return self.logical_scans - self.kernel_scans

    @property
    def avoided(self) -> tuple[str, ...]:
        """Avoid members that fired at this seek's landing.

        This is derived from the typed event evidence rather than stored beside
        it, so simultaneous target/avoid landings cannot disagree about what
        the coast actually observed.
        """

        return tuple(
            event.name
            for event in self.events
            if event.kind == AVOID and event.scan == self.end_scan
        )


def _fold_metadata(
    triggers: Iterable[CoastTrigger],
) -> tuple[
    dict[str, tuple[tuple[str, Any], ...]] | None,
    frozenset[str],
    frozenset[str],
    frozenset[str],
]:
    """Merge crossing thresholds + protected reads from every compiled condition.

    Watched tags are always protected even without a condition — a receipt
    cannot record a transition the fold skipped.
    """
    from pyrung.core.fold import _extract_condition_crossings, _extract_condition_reads
    from pyrung.core.system_points import _CLOCK_HALF_PERIODS, _SCAN_DERIVED_NAMES

    crossings: dict[str, tuple[tuple[str, Any], ...]] = {}
    reads: set[str] = set()
    for trigger in triggers:
        reads.update(trigger.watched)
        if trigger.condition is None:
            continue
        for tag, cmps in _extract_condition_crossings(trigger.condition).items():
            crossings[tag] = crossings.get(tag, ()) + cmps
        reads |= _extract_condition_reads(trigger.condition)

    clock_reads = frozenset(reads) & frozenset(_CLOCK_HALF_PERIODS)
    scan_derived = frozenset(reads) & frozenset(_SCAN_DERIVED_NAMES)
    protected = frozenset(reads) - clock_reads - scan_derived
    return (crossings or None), protected, clock_reads, scan_derived


@dataclass
class CoastSession:
    """A sequence of seeks over one PLC, accumulating an event timeline.

    Owns no machine state and installs nothing — it arms, runs, observes,
    and hands back values.  One session per coast; receipts are the only
    thing that outlives it.
    """

    plc: PLC
    kind: str = "coast"
    # ``None`` preserves live PILOT's automatic kernel-budget policy when
    # reactive holds are installed. Recorded incident replay sets ``False``:
    # its window is measured in historical logical scans.
    kernel_budget: bool | None = None
    # Armed pens: tag -> last recorded value.  A pen is a nonterminal,
    # re-arming change recorder — the literal trend-recorder pen.  During a
    # seek the pens ride as one internal nonterminal trigger (their tags are
    # fold-protected, so every transition is an exact landing); during
    # step-mode ops (dwell / settle / a caller's raw pulse scans) the caller
    # ticks :meth:`note_pens` once per scan.  Pens never end a seek — they
    # only write the timeline.
    pens: dict[str, Any] = field(default_factory=dict)
    _events: list[CoastTriggerEvent] = field(default_factory=list)
    _last_cyclefold_stats: dict[str, int] = field(default_factory=dict)
    _avoid_triggers: tuple[CoastTrigger, ...] = field(default=(), init=False, repr=False)

    @property
    def events(self) -> tuple[CoastTriggerEvent, ...]:
        """The session timeline so far — ordered, same-scan groups preserved."""
        return tuple(self._events)

    def arm_pens(self, tags: Iterable[str]) -> None:
        """Arm a change pen on each tag, baselined at the current value.

        Re-arming an already-armed pen keeps its existing baseline (the pen
        is mid-stroke, not reset).  Callers own the universe; a per-scan-churny
        tag (a raw accumulator) must never be a pen — it would collapse every
        fold to step-mode.
        """
        state_tags = self.plc.state.tags
        for t in tags:
            self.pens.setdefault(t, state_tags.get(t))

    def note_pens(self) -> None:
        """Record one timeline event for every pen that moved since its baseline.

        Step-mode counterpart of the seek-time pen trigger: one
        ``CoastTriggerEvent`` per
        scan carrying all simultaneous transitions (same-scan groups are one
        pen mark, never collapsed with a neighbor scan's).
        """
        if not self.pens:
            return
        state = self.plc.state
        transitions = tuple(
            (t, held, state.tags.get(t))
            for t, held in self.pens.items()
            if not _values_match(held, state.tags.get(t))
        )
        if not transitions:
            return
        self._events.append(CoastTriggerEvent("pen", PEN, state.scan_id, transitions))
        for t, _, after in transitions:
            self.pens[t] = after

    def arm_avoid(self, avoid: Any) -> None:
        """Arm clear ``avoid=`` members for folded seeks.

        Arming is a trial-start decision.  A member already true at that point
        is intentionally omitted so the trial may leave an avoided state.
        Opaque callables are armed without fold metadata: they are checked on
        real kernel scans while condition-like members additionally constrain
        skipped logical spans.
        """

        members = getattr(avoid, "members", ()) if avoid is not None else ()
        start = dict(self.plc.state.tags)
        triggers: list[CoastTrigger] = []
        for member in members:
            condition = getattr(member, "condition", None)
            # Compiled members have an exact read-set. Opaque callables do not,
            # so they retain the historical full-snapshot contract.
            declared_reads = tuple(sorted(member.tags)) if condition is not None else None
            try:
                already_true = bool(member.pred(start))
            except Exception:
                already_true = False
            if already_true:
                continue

            def _pred(
                state: Any,
                _member: Any = member,
                _declared_reads: tuple[str, ...] | None = declared_reads,
            ) -> bool:
                try:
                    tags = state.tags
                    snapshot = (
                        dict(tags)
                        if _declared_reads is None
                        else {name: tags[name] for name in _declared_reads if name in tags}
                    )
                    return bool(_member.pred(snapshot))
                except Exception:
                    return False

            triggers.append(
                CoastTrigger(
                    name=member.name,
                    kind=AVOID,
                    predicate=_pred,
                    condition=condition,
                    watched=tuple(sorted(member.tags)),
                )
            )
        self._avoid_triggers = tuple(triggers)

    def _pen_trigger(self) -> CoastTrigger:
        """The armed pens as one nonterminal trigger for :meth:`seek`.

        The predicate reads the live ``pens`` baselines, so a re-armed pen
        (seek's nonterminal refresh) is immediately consistent.
        """
        pens = self.pens

        def _pred(s: Any) -> bool:
            return any(not _values_match(held, s.tags.get(t)) for t, held in pens.items())

        from pyrung.core.condition import AnyCondition

        condition = _departure_condition(self.plc, dict(pens), {})
        if condition is not None and not isinstance(condition, AnyCondition):
            condition = AnyCondition(condition)
        return CoastTrigger(
            name="pen",
            kind=PEN,
            predicate=_pred,
            condition=condition,
            watched=tuple(pens),
            terminal=False,
        )

    def seek(self, triggers: Iterable[CoastTrigger], *, budget: int) -> CoastReceipt:
        """Coast until the first armed terminal trigger fires; return the receipt.

        Uses the layered ``cycle_fold_until`` engine for every seek.  It tries
        the ordinary plateau/crossing fold first (over the full synthesis +
        program rung surface), then adds a cycle-preserving macro skip when
        changing inner state defeats the ordinary plateau proof.
        """
        plc = self.plc
        armed: list[CoastTrigger] = [*triggers, *self._avoid_triggers]
        if not armed:
            raise ValueError("seek() requires at least one coast trigger")
        if self.pens:
            armed.append(self._pen_trigger())
        start_scan = plc.state.scan_id
        # Pen baselines predate the seek (they carry from the session's last
        # note); other watched tags baseline at the current value.
        baseline: dict[str, Any] = dict(self.pens)
        for trigger in armed:
            for t in trigger.watched:
                baseline.setdefault(t, plc.state.tags.get(t))

        kernel_scans = 0
        macro_folds = 0
        timer_quanta_replayed = 0
        advances: list[tuple[str, Any]] = []
        stop_reason = "timeout"
        fired_terminal: tuple[str, ...] = ()

        # After a nonterminal (pen) firing steps the world, the next armed trigger
        # may already be true at that very scan — judge it BEFORE folding
        # again, or the fold's advance-≥1-before-judging would land one scan
        # late and a cascading transition could carry the machine past the
        # crossing the legacy (pen-less) coast landed on exactly.
        judge_before_run = False
        while True:
            elapsed = plc.state.scan_id - start_scan
            remaining = budget - elapsed
            if remaining <= 0:
                break
            sterile = False
            state = plc.state
            now_fired = (
                [trigger for trigger in armed if trigger.predicate(state)]
                if judge_before_run
                else []
            )
            judge_before_run = False
            if not now_fired:
                # Rebuild the pen condition from its live baselines every time
                # it re-arms; its predicate already reads the same mutable map.
                live = [
                    self._pen_trigger() if trigger.kind == PEN else trigger for trigger in armed
                ]
                crossings, protected, clock_reads, scan_derived = _fold_metadata(live)

                def _any_pred(s: Any, _live: list[CoastTrigger] = live) -> bool:
                    return any(trigger.predicate(s) for trigger in _live)

                declared_predicate_reads = (
                    protected | clock_reads
                    if all(trigger.condition is not None for trigger in live)
                    else None
                )

                # NOTE(phase 4): like the legacy coasts (run_until semantics), a
                # seek always advances at least one scan before judging — a trigger
                # already true at arm time lands after one scan, not zero.  The
                # immediate-landing rule ("a target stops the scan it holds")
                # arrives with the golden regeneration in the verify/outcome phase.
                from pyrung.core.analysis.pilot.cyclefold import cycle_fold_until

                stats: dict[str, int] = {}
                cycle_fold_until(
                    plc,
                    _any_pred,
                    budget=remaining,
                    kernel_budget=self.kernel_budget,
                    fold_ctx=plc._ensure_fold_context(protected, clock_reads, scan_derived),
                    extra_comparisons=crossings,
                    predicate_reads=declared_predicate_reads,
                    stats=stats,
                    advances=advances,
                )
                kernel_scans += stats.get("kernel_scans", 0)
                macro_folds += stats.get("macro_folds", 0)
                timer_quanta_replayed += stats.get("timer_quanta_replayed", 0)
                self._last_cyclefold_stats = stats
                # A certified sterile cycle is a *proof* no armed trigger can
                # ever fire — the strongest form of timeout, arrived early.
                sterile = bool(stats.get("sterile_cycle"))

                state = plc.state
                now_fired = [trigger for trigger in armed if trigger.predicate(state)]
                if not now_fired:
                    elapsed = state.scan_id - start_scan
                    stop_reason = "timeout" if sterile or elapsed >= budget else "paused"
                    break

            scan = state.scan_id
            for trigger in now_fired:
                transitions = tuple(
                    (t, baseline.get(t), state.tags.get(t))
                    for t in trigger.watched
                    if not _values_match(baseline.get(t), state.tags.get(t))
                )
                self._events.append(
                    CoastTriggerEvent(trigger.name, trigger.kind, scan, transitions)
                )
            # Refresh every fired trigger's watched baseline AFTER all events are
            # recorded (two triggers watching one tag must both see the old value)
            # and BEFORE the terminal check, so a terminal exit leaves the
            # session pens current — the next session op must not re-record a
            # transition the terminal landing already wrote down.
            for trigger in now_fired:
                for t in trigger.watched:
                    baseline[t] = state.tags.get(t)
                    if trigger.kind == PEN:
                        self.pens[t] = state.tags.get(t)

            terminal = [trigger for trigger in now_fired if trigger.terminal]
            if terminal:
                fired_terminal = tuple(trigger.name for trigger in terminal)
                kinds = {trigger.kind for trigger in terminal}
                if TARGET in kinds:
                    stop_reason = "reached"
                elif DEPARTURE in kinds:
                    stop_reason = "departed"
                else:
                    stop_reason = terminal[0].kind
                break

            # All firings nonterminal: re-arm (or disarm one-shots) and keep
            # coasting — baselines were already refreshed above.
            for trigger in now_fired:
                if trigger.one_shot:
                    armed.remove(trigger)
            if not armed:
                stop_reason = "departed"
                break
            # A nonterminal trigger still true next scan would spin the loop
            # without motion; step once so the world moves past the firing,
            # then judge that scan directly on the next pass.
            plc.step()
            kernel_scans += 1
            judge_before_run = True

        # A timeout can break out after a step the loop never judged, and an
        # external pause can stop the fold between landings — write down any
        # pen drift the loop didn't get to evaluate (exact scan for the
        # timeout case; the pause case attributes to the pause scan).
        self.note_pens()

        receipt = CoastReceipt(
            kind=self.kind,
            start_scan=start_scan,
            end_scan=plc.state.scan_id,
            stop_reason=stop_reason,
            fired=fired_terminal,
            events=tuple(self._events),
            budget=budget,
            kernel_scans=kernel_scans,
            macro_folds=macro_folds,
            advances=tuple(advances),
            timer_quanta_replayed=timer_quanta_replayed,
        )
        if logger.isEnabledFor(logging.DEBUG):
            logger.debug(
                "coast %s: %s at scan %d "
                "(%d logical scans, %d kernel scans, %d skipped, %d macro folds, "
                "%d timer quanta replayed) "
                "fired=%s%s",
                self.kind,
                receipt.stop_reason,
                receipt.end_scan,
                receipt.logical_scans,
                receipt.kernel_scans,
                receipt.skipped_scans,
                receipt.macro_folds,
                receipt.timer_quanta_replayed,
                ",".join(receipt.fired) or "-",
                f" cyclefold={self._last_cyclefold_stats}" if self._last_cyclefold_stats else "",
            )
        return receipt

    def dwell(self, scans: int) -> CoastReceipt:
        """Run exactly *scans* real scans — a fixed dwell, not a seek.

        The one waiting shape with no predicate (a pulse's fixed settle
        window): explicit by design, never disguised as a trigger.
        """
        plc = self.plc
        start_scan = plc.state.scan_id
        for _ in range(scans):
            plc.step()
            self.note_pens()
        return CoastReceipt(
            kind=self.kind,
            start_scan=start_scan,
            end_scan=plc.state.scan_id,
            stop_reason="dwell",
            fired=(),
            events=tuple(self._events),
            budget=scans,
            kernel_scans=scans,
        )

    def settle_landing(
        self,
        channel_tag: str,
        *,
        confirm_scans: int = LIMITS.landing_confirm_scans,
        cap: int = LIMITS.landing_cap,
    ) -> CoastReceipt:
        """Ride a departure's transition chain to its stable landing.

        Departure-then-quiescence: arm a departure trigger off the channel's
        current value with a *confirm_scans* budget.  A silent window is the
        landing (``"quiescent"``); a departure is the next hop — record it,
        re-arm off the new value, and keep riding, bounded by *cap* total
        scans (``"timeout"`` — a cap-hit value may be mid-transition and
        must never be classified as settled).

        The confirmation window is the old 100-stable-scans policy kept as
        policy, not as a stepping loop: the seek folds through the silence
        (one fold for a quiet second; cyclefold when oscillating holds are
        active), and every intermediate hop lands exactly and is recorded
        on the session timeline — the transition chain becomes evidence.
        """
        plc = self.plc
        start_scan = plc.state.scan_id
        stop_reason = "timeout"
        kernel_scans = 0
        macro_folds = 0
        timer_quanta_replayed = 0
        while True:
            remaining = cap - (plc.state.scan_id - start_scan)
            if remaining <= 0:
                break
            held = plc.state.tags.get(channel_tag)
            receipt = self.seek(
                [departure_trigger(plc, "hop", {channel_tag: held})],
                budget=min(confirm_scans, remaining),
            )
            kernel_scans += receipt.kernel_scans
            macro_folds += receipt.macro_folds
            timer_quanta_replayed += receipt.timer_quanta_replayed
            if receipt.stop_reason == "timeout":
                # Silent through the whole confirmation window: landed.
                stop_reason = "quiescent"
                break
            if receipt.stop_reason != "departed":
                stop_reason = receipt.stop_reason
                break
        return CoastReceipt(
            kind=self.kind,
            start_scan=start_scan,
            end_scan=plc.state.scan_id,
            stop_reason=stop_reason,
            fired=(),
            events=tuple(self._events),
            budget=cap,
            kernel_scans=kernel_scans,
            macro_folds=macro_folds,
            timer_quanta_replayed=timer_quanta_replayed,
        )

    def settle(
        self,
        watched_tags: frozenset[str],
        *,
        floor: int = LIMITS.cone_floor,
        ceiling: int = LIMITS.cone_ceiling,
        reached_fn: Callable[[dict[str, Any]], bool] | None = None,
    ) -> CoastReceipt:
        """Step scan-by-scan until the watched tags stop moving.

        Quiescence, not silence-for-N: stop the first scan (after *floor*)
        that no watched tag changed since the previous scan — a watched-tag
        fixpoint.  Deliberately step-mode: the fixpoint compares consecutive
        real scans, which a fold would compress away; the ceiling keeps the
        window small (the fold handles long dwells via :meth:`seek`).

        ``reached_fn`` (over the tags dict) short-circuits the dwell so a
        one-scan transient target (STARTING for a single scan on the way to
        EXECUTE) is landed on, not blown past.

        ``stop_reason``: ``"reached"``, ``"quiescent"``, or ``"timeout"`` —
        a non-quiesced ceiling exit is *named*, never passed off as settled.
        The receipt's ``trajectory`` carries the per-scan snapshots.
        """
        plc = self.plc
        start_scan = plc.state.scan_id
        ceiling = max(floor, ceiling)
        snaps: list[dict[str, Any]] = []
        stop_reason = "timeout"
        prev = dict(plc.state.tags)
        for i in range(ceiling):
            plc.step()
            self.note_pens()
            cur = dict(plc.state.tags)
            snaps.append(cur)
            if reached_fn is not None and reached_fn(cur):
                stop_reason = "reached"
                break
            if i + 1 >= floor and all(cur.get(t) == prev.get(t) for t in watched_tags):
                stop_reason = "quiescent"
                break
            prev = cur
        return CoastReceipt(
            kind=self.kind,
            start_scan=start_scan,
            end_scan=plc.state.scan_id,
            stop_reason=stop_reason,
            fired=(),
            events=tuple(self._events),
            budget=ceiling,
            kernel_scans=len(snaps),
            trajectory=tuple(snaps),
        )


def value_trigger(
    plc: PLC,
    name: str,
    kind: str,
    tag_name: str,
    value: Any,
    *,
    terminal: bool = True,
) -> CoastTrigger:
    """``tag == value`` trigger: authoritative ``_values_match`` predicate plus a
    compiled ``CompareEq`` condition for fold metadata when the tag is known."""

    def _pred(s: Any) -> bool:
        return _values_match(s.tags.get(tag_name), value)

    condition = None
    tag = plc._known_tags_by_name.get(tag_name)
    if tag is not None and isinstance(value, (bool, int, float, str)):
        from pyrung.core.condition import CompareEq

        condition = CompareEq(tag, value)
    return CoastTrigger(
        name=name,
        kind=kind,
        predicate=_pred,
        condition=condition,
        watched=(tag_name,),
        terminal=terminal,
    )


def departure_trigger(
    plc: PLC,
    name: str,
    holds: dict[str, Any],
    *,
    excluding: dict[str, Any] | None = None,
    terminal: bool = True,
) -> CoastTrigger:
    """Any held tag leaves its value — the ejection guard as an armed trigger.

    ``excluding`` maps a tag to a value that does NOT count as a departure
    (the bearing coast's own target: reaching it is arrival, not ejection).
    """
    excluding = excluding or {}

    def _pred(s: Any) -> bool:
        for t, held in holds.items():
            cur = s.tags.get(t)
            if _values_match(cur, held):
                continue
            skip = excluding.get(t)
            if skip is not None and _values_match(cur, skip):
                continue
            return True
        return False

    condition = _departure_condition(plc, holds, excluding)
    return CoastTrigger(
        name=name,
        kind=DEPARTURE,
        predicate=_pred,
        condition=condition,
        watched=tuple(holds),
        terminal=terminal,
    )


def _departure_condition(plc: PLC, holds: dict[str, Any], excluding: dict[str, Any]) -> Any | None:
    """Compile the departure predicate for fold metadata; None when any leg
    can't compile (the predicate stays authoritative either way)."""
    from pyrung.core.condition import AllCondition, AnyCondition, CompareNe

    legs = []
    for t, held in holds.items():
        tag = plc._known_tags_by_name.get(t)
        if tag is None or not isinstance(held, (bool, int, float, str)):
            return None
        leg: Any = CompareNe(tag, held)
        skip = excluding.get(t)
        if skip is not None:
            if not isinstance(skip, (bool, int, float, str)):
                return None
            leg = AllCondition(leg, CompareNe(tag, skip))
        legs.append(leg)
    if not legs:
        return None
    return legs[0] if len(legs) == 1 else AnyCondition(*legs)


def predicate_trigger(
    name: str,
    kind: str,
    predicate: Callable[[Any], bool],
    *,
    condition: Any = None,
    watched: tuple[str, ...] = (),
    terminal: bool = True,
) -> CoastTrigger:
    """Callable trigger with optional equivalent Condition fold metadata."""
    return CoastTrigger(
        name=name,
        kind=kind,
        predicate=predicate,
        condition=condition,
        watched=watched,
        terminal=terminal,
    )


def coast_departure_tags(state: Any, ctx: Any) -> tuple[str, ...]:
    """Channels whose departure terminates a coast holding the current world.

    Pipeline analysis owns recognized request/state channels. EarnedWork owns
    monotone progress coordinates. An exact stateful target with no EarnedWork
    owner is itself a discrete channel, even when the program has no inferred
    operator-request pipeline. Keeping that arbitration here gives coast,
    VERIFY replay, and investigation the same channel set.
    """
    channels = list(dict.fromkeys(role.channel_tag for role in ctx.pipeline_roles))
    config = state.key_config
    target = ctx.target.tag
    earned_work_tags = {
        component.tag
        for component in getattr(getattr(state, "earned_work", None), "components", ())
    }
    if (
        ctx.target.predicate is None
        and config is not None
        and target in config.stateful_names
        and target not in earned_work_tags
        and target not in channels
    ):
        channels.append(target)
    return tuple(channels)


def _coast_to_value(
    plc: PLC,
    channel_tag: str | None,
    target_value: Any,
    *,
    budget: int = _COAST_BUDGET,
    session: Any = None,
) -> CoastReceipt:
    """Coast *plc* forward (folding) until ``channel_tag == target_value``.

    Arms two coast triggers — the target and a departure (the channel leaving
    its start value for anything but the target) — so the coast lands on the
    exact scan either fires and the receipt says which. This is the single
    mechanism for "hold heading and let scans pass": the live bearing coast
    (``steer``) and the investigation replay (``investigate``) both coast
    through timer dwell identically, so a replay reproduces the live coast.

    Conditional holds animate during the coast exactly as in
    :func:`_coast_holding_state` — a confirmed oscillation corrective (a
    watchdog pet) that only the terminal let-run animated would silently drop
    out of every coast, re-tripping the watchdog it exists to feed.

    ``receipt.stop_reason == "reached"`` means the target was reached without
    ejection.
    """
    if channel_tag is None:
        return CoastReceipt(
            kind=session.kind if session is not None else "bearing_coast",
            start_scan=plc.state.scan_id,
            end_scan=plc.state.scan_id,
            stop_reason="skipped",
            fired=(),
            events=(),
            budget=0,
        )

    start = plc.state.tags.get(channel_tag)
    triggers = [
        value_trigger(plc, "target", TARGET, channel_tag, target_value),
        departure_trigger(
            plc,
            "ejected",
            {channel_tag: start},
            excluding={channel_tag: target_value},
        ),
    ]
    if session is None:
        session = CoastSession(plc, kind="bearing_coast")
    assert session.plc is plc
    return session.seek(triggers, budget=budget)


def _coast_holding_state(
    plc: PLC,
    target_tag: str,
    target_value: Any,
    role_tags: tuple[str, ...],
    *,
    budget: int = _COAST_BUDGET,
    reached_fn: Callable[[Any], bool] | None = None,
    reached_condition: Any = None,
    session: Any = None,
) -> CoastReceipt:
    """Coast toward the global target while holding the current macro-state.

    *reached_fn* overrides the stop condition — supply it for a relational
    target (``Temp >= 5.0``), where the goal is the predicate holding, not the
    register hitting an exact ``target_value``. Defaults to exact-value match.

    Heading is the global target itself — no intermediate bearing or channel
    register is assumed. The ejection guard is "the macro-state I am parked in
    changed on its own": any recognized state-machine role register
    (``role_tags``) leaving the value it held at coast start pauses the coast at
    that scan, so an ejection (Execute -> Aborting) hands a tight incident to
    investigation instead of burning the whole budget.

    With no roles (a program without a recognized state machine) the departure
    trigger never fires and the coast simply runs to the target or the budget —
    still safe.

    ``receipt.stop_reason == "reached"`` means the target was reached without
    ejection.
    """
    if reached_fn is not None:
        target = predicate_trigger(
            "target",
            TARGET,
            reached_fn,
            condition=reached_condition,
            watched=(target_tag,),
        )
    else:
        target = value_trigger(plc, "target", TARGET, target_tag, target_value)

    triggers = [target]
    if role_tags:
        start = {tag: plc.state.tags.get(tag) for tag in role_tags}
        triggers.append(departure_trigger(plc, "ejected", start))
    if session is None:
        session = CoastSession(plc, kind="letrun")
    assert session.plc is plc
    return session.seek(triggers, budget=budget)


def _settle_delayed_effects(
    fork: PLC,
    *,
    scan_budget: int = 2000,
    session: Any = None,
) -> list[CoastReceipt]:
    """Settle environment-owned latency after an intervention.

    If the harness has scheduled patches (Physical on_delay/off_delay), seek
    harness quiescence (``pending_count == 0``), then dwell one scan — the plant
    commits feedback the scan it settles; the program that reads it reacts the
    next scan (the scan boundary is the plant latency).

    Program instruction progress is deliberately not settled here. A newly
    armed timer/counter/drum is a distinct operation owned by its
    :class:`AdvanceProfile`; trace/program-step must re-read that owner and
    prescribe the observable boundary as an ordinary coast. Fast-forwarding
    timing bits here used to execute that operation a second time, invisibly,
    before option ordering or correction lifecycle could observe it.
    """
    budget = scan_budget
    receipts: list[CoastReceipt] = []
    if session is None:
        session = CoastSession(fork, kind="delayed-effects")
    assert session.plc is fork

    harness = getattr(fork, "_harness", None)
    if harness is not None and harness.pending_count > 0:
        scan_before = fork.state.scan_id
        receipt = session.seek(
            [
                predicate_trigger(
                    "harness_quiescent",
                    QUIESCENT,
                    lambda state: harness.pending_count == 0,
                )
            ],
            budget=budget,
        )
        receipts.append(receipt)
        if harness.pending_count == 0 and fork.state.scan_id - scan_before < budget:
            session.dwell(1)
    return receipts


def _has_pending_effects(fork: PLC) -> bool:
    """True if the fork has unsettled harness feedback.

    Bool dwell reports via ``pending_count``; an analog coupling is "pending"
    while its enable is active — its plant rung is still driving the feedback
    register this scan.
    """
    harness = getattr(fork, "_harness", None)
    if harness is None:
        return False
    if harness.pending_count > 0:
        return True
    snap = fork.current_state.tags
    for coupling in getattr(harness, "_profile_couplings", ()):
        en_raw = snap.get(coupling.en_name, False)
        enabled = (
            en_raw == coupling.trigger_value if coupling.trigger_value is not None else bool(en_raw)
        )
        if enabled:
            return True
    return False
