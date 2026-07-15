"""CoastSession — bump-driven coasts with receipts (the technician's trend recorder).

WWTD: a tech at a fault doesn't stare at a frozen screen and guess; they put a
trend recorder on the registers that matter and read the pen marks.  A *bump*
is one armed pen: a named predicate over machine state.  A *seek* coasts the
machine (folding) until the first armed bump fires and lands on that exact
scan.  The *receipt* records what was observed — which bump, when, what moved —
so callers judge evidence instead of re-deriving it from history.

Three perfections, each a property the fold machinery already guarantees:

- **Perfect reaction** — the fold stops one scan short of the nearest crossing
  and executes the landing as a real probe scan; a bump is never overshot.
- **Perfect recall** — nonterminal bumps re-arm and append to an ordered
  timeline; simultaneous terminal bumps are all recorded, never collapsed.
- **Perfect tracing** — every landing is a real, fully-recorded scan, so
  ``rung_firings`` / ``cause()`` attribution is exact there (and only there).

Authority split (the behavior-neutrality invariant): each bump's **predicate
callable is authoritative** — it decides truth with the same ``_values_match``
semantics the legacy coasts used.  The optional compiled **condition supplies
fold metadata only** (crossing thresholds + protected reads), so the fold lands
exactly without the predicate's semantics ever drifting.  A bump with no
condition still works; it just leans on the plateau guard alone (the documented
opaque-callable fallback).

Depends only on the PLC/fold interface — never on pilot loop logic, verify
gates, or investigation (same layer as ``_ops.py``, which imports this module,
never the reverse).
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


@dataclass(frozen=True)
class CoastLimits:
    """The named policy horizons, centralized.  Values only — the *decision*
    of when each applies stays with its owner (one owner per decision)."""

    zoom_budget: int = 10_000
    cone_floor: int = 2  # ladder logic takes ≤2 scans to propagate
    cone_ceiling: int = 16
    dwell_ceiling: int = 64
    delayed_effects_budget: int = 2_000
    pulse_settle_scans: int = 4


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

    @property
    def reached(self) -> bool:
        return self.stop_reason == "reached"


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
    _events: list[BumpEvent] = field(default_factory=list)

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
        start_scan = plc.state.scan_id
        baseline: dict[str, Any] = {}
        for b in armed:
            for t in b.watched:
                baseline.setdefault(t, plc.state.tags.get(t))

        crossings, protected, clock_reads, scan_derived = _fold_metadata(armed)
        active_rungs = bool(plc._synthesis is not None and plc._synthesis.holds)
        real_scans = 0
        folds = 0
        stop_reason = "timeout"
        fired_terminal: tuple[str, ...] = ()

        while True:
            live = list(armed)

            def _any_pred(s: Any, _live: list[Bump] = live) -> bool:
                return any(b.predicate(s) for b in _live)

            elapsed = plc.state.scan_id - start_scan
            remaining = budget - elapsed
            if remaining <= 0:
                break
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
                    stats=stats,
                )
                real_scans += stats.get("real_scans", 0)
                folds += stats.get("folds", 0)
            else:
                from pyrung.core.fold import fold_run_until

                fold_run_until(
                    plc,
                    _any_pred,
                    max_cycles=remaining,
                    fold_ctx=plc._ensure_fold_context(protected, clock_reads, scan_derived),
                    extra_comparisons=crossings,
                )

            state = plc.state
            now_fired = [b for b in armed if b.predicate(state)]
            if not now_fired:
                elapsed = state.scan_id - start_scan
                stop_reason = "timeout" if elapsed >= budget else "paused"
                break

            scan = state.scan_id
            for b in now_fired:
                transitions = tuple(
                    (t, baseline.get(t), state.tags.get(t))
                    for t in b.watched
                    if not _values_match(baseline.get(t), state.tags.get(t))
                )
                self._events.append(BumpEvent(b.name, b.kind, scan, transitions))

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

            # All firings nonterminal: re-arm (or disarm one-shots), refresh
            # each fired bump's watched baseline so its next event records the
            # next transition, and keep coasting.
            for b in now_fired:
                if b.one_shot:
                    armed.remove(b)
                for t in b.watched:
                    baseline[t] = state.tags.get(t)
            if not armed:
                stop_reason = "departed"
                break
            # A nonterminal bump still true next scan would spin the loop
            # without motion; step once so the world moves past the firing.
            plc.step()
            real_scans += 1

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
        )
        if logger.isEnabledFor(logging.DEBUG):
            logger.debug(
                "coast %s: %s at scan %d (%d scan-ids, %d real scans, %d folds) fired=%s",
                self.kind,
                receipt.stop_reason,
                receipt.end_scan,
                receipt.end_scan - receipt.start_scan,
                receipt.real_scans,
                receipt.folds,
                ",".join(receipt.fired) or "-",
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
