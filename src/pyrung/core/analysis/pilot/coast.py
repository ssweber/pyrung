"""Run bump-driven coasts and return exact observation receipts.

A ``Bump`` is a named state predicate. ``CoastSession`` advances with folding
when safe, lands each crossing on a real recorded scan, re-arms nonterminal
bumps, and records simultaneous terminal bumps in a ``CoastReceipt``.

The predicate callable decides whether a bump fired. An optional compiled
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

# Bump kinds are strings, not an enum — the vocabulary grows per cutover phase
# and consumers match on names they know.
TARGET = "target"
DEPARTURE = "departure"
QUIESCENT = "quiescent"
PEN = "pen"


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


@dataclass(frozen=True)
class Bump:
    """One armed pen on the trend recorder.

    ``predicate`` is authoritative.  ``condition`` (a compiled ``Condition``)
    is fold metadata only: its comparison atoms become crossing targets and
    its reads become fold-protected tags.  ``watched`` names the tags whose
    transitions the receipt records when this bump fires.  ``terminal`` bumps
    end the seek; nonterminal bumps record an event and re-arm (``one_shot``
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
class BumpEvent:
    """One pen mark: a bump firing at an exact scan."""

    name: str
    kind: str
    scan: int
    transitions: tuple[tuple[str, Any, Any], ...]  # (tag, before, after)


@dataclass(frozen=True)
class CoastReceipt:
    """What one seek observed.  Values only — safe to carry across reverts.

    ``stop_reason``: ``"reached"`` (a target bump fired), ``"departed"`` (a
    departure fired without a target), ``"quiescent"`` (a quiescence bump or
    cone fixpoint), ``"timeout"`` (budget/ceiling exhausted, nothing fired),
    ``"paused"`` (an external pause stopped the coast early), ``"dwell"``
    (a fixed dwell completed), or ``"skipped"`` (nothing to coast).
    ``fired`` names every terminal bump true at the landing scan —
    simultaneous firings are all present.  ``trajectory`` is populated only
    by :meth:`CoastSession.settle` (per-scan snapshots of the dwell).
    """

    kind: str
    start_scan: int
    end_scan: int
    stop_reason: str
    fired: tuple[str, ...]
    events: tuple[BumpEvent, ...]
    budget: int
    real_scans: int = 0
    folds: int = 0
    trajectory: tuple[dict[str, Any], ...] = ()
    # Exact accumulator destinations written by cycle folding, in execution
    # order. These are the manual edits needed to reproduce each jump ahead.
    advances: tuple[tuple[str, Any], ...] = ()

    @property
    def reached(self) -> bool:
        return self.stop_reason == "reached"

    @property
    def logical_scans(self) -> int:
        """Logical scan IDs advanced, including scans skipped by folds."""
        return self.end_scan - self.start_scan

    @property
    def kernel_scans(self) -> int:
        """Actual interpreter executions (the legacy ``real_scans`` field)."""
        return self.real_scans

    @property
    def macro_folds(self) -> int:
        """Macro-fold executions (the legacy ``folds`` field)."""
        return self.folds

    @property
    def skipped_scans(self) -> int:
        """Logical scans advanced without an interpreter execution."""
        return self.logical_scans - self.kernel_scans


def _fold_metadata(
    bumps: Iterable[Bump],
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
    for b in bumps:
        reads.update(b.watched)
        if b.condition is None:
            continue
        for tag, cmps in _extract_condition_crossings(b.condition).items():
            crossings[tag] = crossings.get(tag, ()) + cmps
        reads |= _extract_condition_reads(b.condition)

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
    # Armed pens: tag -> last recorded value.  A pen is a nonterminal,
    # re-arming change recorder — the literal trend-recorder pen.  During a
    # seek the pens ride as one internal nonterminal bump (their tags are
    # fold-protected, so every transition is an exact landing); during
    # step-mode ops (dwell / settle / a caller's raw pulse scans) the caller
    # ticks :meth:`note_pens` once per scan.  Pens never end a seek — they
    # only write the timeline.
    pens: dict[str, Any] = field(default_factory=dict)
    _events: list[BumpEvent] = field(default_factory=list)
    _last_cyclefold_stats: dict[str, int] = field(default_factory=dict)

    @property
    def events(self) -> tuple[BumpEvent, ...]:
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

        Step-mode counterpart of the seek-time pen bump: one BumpEvent per
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
        self._events.append(BumpEvent("pen", PEN, state.scan_id, transitions))
        for t, _, after in transitions:
            self.pens[t] = after

    def _pen_bump(self) -> Bump:
        """The armed pens as one nonterminal bump for :meth:`seek`.

        The predicate reads the live ``pens`` baselines, so a re-armed pen
        (seek's nonterminal refresh) is immediately consistent.
        """
        pens = self.pens

        def _pred(s: Any) -> bool:
            return any(not _values_match(held, s.tags.get(t)) for t, held in pens.items())

        return Bump(name="pen", kind=PEN, predicate=_pred, watched=tuple(pens), terminal=False)

    def seek(self, bumps: Iterable[Bump], *, budget: int) -> CoastReceipt:
        """Coast until the first armed terminal bump fires; return the receipt.

        Uses ``cycle_fold_until`` when active oscillating holds are present
        (the pet-timer soak must run every scan; the dt-knob would over-advance
        the very timer the oscillation keeps reset) and the runner fold
        otherwise — the same dispatch the legacy coasts used, now with the
        bump vector's fold metadata threaded into both engines.
        """
        plc = self.plc
        armed: list[Bump] = list(bumps)
        if not armed:
            raise ValueError("seek() requires at least one bump")
        if self.pens:
            armed.append(self._pen_bump())
        start_scan = plc.state.scan_id
        # Pen baselines predate the seek (they carry from the session's last
        # note); other watched tags baseline at the current value.
        baseline: dict[str, Any] = dict(self.pens)
        for b in armed:
            for t in b.watched:
                baseline.setdefault(t, plc.state.tags.get(t))

        crossings, protected, clock_reads, scan_derived = _fold_metadata(armed)
        active_rungs = bool(plc._synthesis is not None and plc._synthesis.holds)
        real_scans = 0
        folds = 0
        advances: list[tuple[str, Any]] = []
        stop_reason = "timeout"
        fired_terminal: tuple[str, ...] = ()

        # After a nonterminal (pen) firing steps the world, the next armed bump
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
            now_fired = [b for b in armed if b.predicate(state)] if judge_before_run else []
            judge_before_run = False
            if not now_fired:
                live = list(armed)

                def _any_pred(s: Any, _live: list[Bump] = live) -> bool:
                    return any(b.predicate(s) for b in _live)

                # NOTE(phase 4): like the legacy coasts (run_until semantics), a
                # seek always advances at least one scan before judging — a bump
                # already true at arm time lands after one scan, not zero.  The
                # immediate-landing rule ("a target stops the scan it holds")
                # arrives with the golden regeneration in the verify/outcome phase.
                if active_rungs:
                    from pyrung.core.analysis.pilot.cyclefold import cycle_fold_until

                    stats: dict[str, int] = {}
                    cycle_fold_until(
                        plc,
                        _any_pred,
                        budget=remaining,
                        fold_ctx=plc._ensure_fold_context(protected, clock_reads, scan_derived),
                        extra_comparisons=crossings,
                        predicate_reads=protected | clock_reads,
                        stats=stats,
                        advances=advances,
                    )
                    real_scans += stats.get("real_scans", 0)
                    folds += stats.get("folds", 0)
                    self._last_cyclefold_stats = stats
                    # A certified sterile cycle is a *proof* no armed bump can
                    # ever fire — the strongest form of timeout, arrived early.
                    sterile = bool(stats.get("sterile_cycle"))
                else:
                    from pyrung.core.fold import fold_run_until

                    stats = {}
                    fold_run_until(
                        plc,
                        _any_pred,
                        max_cycles=remaining,
                        fold_ctx=plc._ensure_fold_context(protected, clock_reads, scan_derived),
                        extra_comparisons=crossings,
                        stats=stats,
                    )
                    real_scans += stats.get("kernel_scans", 0)
                    folds += stats.get("macro_folds", 0)

                state = plc.state
                now_fired = [b for b in armed if b.predicate(state)]
                if not now_fired:
                    elapsed = state.scan_id - start_scan
                    stop_reason = "timeout" if sterile or elapsed >= budget else "paused"
                    break

            scan = state.scan_id
            for b in now_fired:
                transitions = tuple(
                    (t, baseline.get(t), state.tags.get(t))
                    for t in b.watched
                    if not _values_match(baseline.get(t), state.tags.get(t))
                )
                self._events.append(BumpEvent(b.name, b.kind, scan, transitions))
            # Refresh every fired bump's watched baseline AFTER all events are
            # recorded (two bumps watching one tag must both see the old value)
            # and BEFORE the terminal check, so a terminal exit leaves the
            # session pens current — the next session op must not re-record a
            # transition the terminal landing already wrote down.
            for b in now_fired:
                for t in b.watched:
                    baseline[t] = state.tags.get(t)
                    if b.kind == PEN:
                        self.pens[t] = state.tags.get(t)

            terminal = [b for b in now_fired if b.terminal]
            if terminal:
                fired_terminal = tuple(b.name for b in terminal)
                kinds = {b.kind for b in terminal}
                if TARGET in kinds:
                    stop_reason = "reached"
                elif DEPARTURE in kinds:
                    stop_reason = "departed"
                else:
                    stop_reason = terminal[0].kind
                break

            # All firings nonterminal: re-arm (or disarm one-shots) and keep
            # coasting — baselines were already refreshed above.
            for b in now_fired:
                if b.one_shot:
                    armed.remove(b)
            if not armed:
                stop_reason = "departed"
                break
            # A nonterminal bump still true next scan would spin the loop
            # without motion; step once so the world moves past the firing,
            # then judge that scan directly on the next pass.
            plc.step()
            real_scans += 1
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
            real_scans=real_scans,
            folds=folds,
            advances=tuple(advances),
        )
        if logger.isEnabledFor(logging.DEBUG):
            logger.debug(
                "coast %s: %s at scan %d "
                "(%d logical scans, %d kernel scans, %d skipped, %d macro folds) "
                "fired=%s%s",
                self.kind,
                receipt.stop_reason,
                receipt.end_scan,
                receipt.logical_scans,
                receipt.kernel_scans,
                receipt.skipped_scans,
                receipt.macro_folds,
                ",".join(receipt.fired) or "-",
                f" cyclefold={self._last_cyclefold_stats}" if self._last_cyclefold_stats else "",
            )
        return receipt

    def dwell(self, scans: int) -> CoastReceipt:
        """Run exactly *scans* real scans — a fixed dwell, not a seek.

        The one waiting shape with no predicate (a pulse's fixed settle
        window): explicit by design, never disguised as a bump.
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
            real_scans=scans,
        )

    def settle_landing(
        self,
        channel_tag: str,
        *,
        confirm_scans: int = LIMITS.landing_confirm_scans,
        cap: int = LIMITS.landing_cap,
    ) -> CoastReceipt:
        """Ride a departure's transition chain to its stable landing.

        Departure-then-quiescence: arm a departure bump off the channel's
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
        real_scans = 0
        folds = 0
        while True:
            remaining = cap - (plc.state.scan_id - start_scan)
            if remaining <= 0:
                break
            held = plc.state.tags.get(channel_tag)
            receipt = self.seek(
                [departure_bump(plc, "hop", {channel_tag: held})],
                budget=min(confirm_scans, remaining),
            )
            real_scans += receipt.real_scans
            folds += receipt.folds
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
            real_scans=real_scans,
            folds=folds,
        )

    def settle(
        self,
        watch: frozenset[str],
        *,
        floor: int = LIMITS.cone_floor,
        ceiling: int = LIMITS.cone_ceiling,
        reached_fn: Callable[[dict[str, Any]], bool] | None = None,
    ) -> CoastReceipt:
        """Step scan-by-scan until the watched cone stops moving.

        Quiescence, not silence-for-N: stop the first scan (after *floor*)
        that no watched tag changed since the previous scan — a cone
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
            if i + 1 >= floor and all(cur.get(t) == prev.get(t) for t in watch):
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
            real_scans=len(snaps),
            trajectory=tuple(snaps),
        )


def value_bump(
    plc: PLC,
    name: str,
    kind: str,
    tag_name: str,
    value: Any,
    *,
    terminal: bool = True,
) -> Bump:
    """``tag == value`` bump: authoritative ``_values_match`` predicate plus a
    compiled ``CompareEq`` condition for fold metadata when the tag is known."""

    def _pred(s: Any) -> bool:
        return _values_match(s.tags.get(tag_name), value)

    condition = None
    tag = plc._known_tags_by_name.get(tag_name)
    if tag is not None and isinstance(value, (bool, int, float, str)):
        from pyrung.core.condition import CompareEq

        condition = CompareEq(tag, value)
    return Bump(
        name=name,
        kind=kind,
        predicate=_pred,
        condition=condition,
        watched=(tag_name,),
        terminal=terminal,
    )


def departure_bump(
    plc: PLC,
    name: str,
    holds: dict[str, Any],
    *,
    excluding: dict[str, Any] | None = None,
    terminal: bool = True,
) -> Bump:
    """Any held tag leaves its value — the ejection guard as an armed bump.

    ``excluding`` maps a tag to a value that does NOT count as a departure
    (the zoom's own target: reaching it is arrival, not ejection).
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
    return Bump(
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


def predicate_bump(
    name: str,
    kind: str,
    predicate: Callable[[Any], bool],
    *,
    watched: tuple[str, ...] = (),
    terminal: bool = True,
) -> Bump:
    """Opaque-callable bump (a relational ``reached_fn``): no fold metadata,
    plateau guard + watched-tag protection only."""
    return Bump(name=name, kind=kind, predicate=predicate, watched=watched, terminal=terminal)
