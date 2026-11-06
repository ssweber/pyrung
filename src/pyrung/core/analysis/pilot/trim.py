"""Temporary logic a reached plan turns out not to need, found by running the plan without it.

The pilot installs temporary logic when the trace says an input has to stay where it is through
a wait.  Often nothing would have moved it: a plain Set holds as well, or the program holds it
itself.  The trace can't always tell, but running the plan can.  So, once a plan has reached,
each piece of temporary logic is tried in the order it went in: the plan is run again from its
anchor without it, and then with a Set of its value in its place.  The simpler plan is kept
only if it still reaches the goal on the same scan, with nothing to avoid on the way.

The kept plan is that new run, so what it says and what it replays are one recording.
"""

from __future__ import annotations

import math
from collections.abc import Callable, Mapping
from dataclasses import replace
from typing import Any

from pyrung.core.analysis.graph import Plan, PlanStep
from pyrung.core.analysis.pilot.avoid import _AvoidCheckRaised
from pyrung.core.analysis.pilot.budget import WallClock
from pyrung.core.analysis.pilot.coast import CoastSession, CoastTrigger
from pyrung.core.analysis.pilot.overlay import PILOT_RUNGS_BUILT_FROM, fork_with_pilot_rungs
from pyrung.core.analysis.pilot.world_key import _rung_identity

# Each try runs the whole plan again.  A plan's own scans mostly run in the guarded loop, so a
# try costs a small part of the search that found it; this keeps a slow one from costing more.
_WALL_S = 2.0

_Change = Mapping[tuple[Any, ...], str]

# A wait between two of the plan's moves ends on its scan count, so nothing ends it.
_NEVER = CoastTrigger(
    name="replayed wait", kind="dwell", predicate=lambda _state: False, predicate_reads=()
)


def _timeline(plan: Plan) -> list[tuple[int, tuple[Any, ...]]] | None:
    """Each scan from the anchor on where the temporary logic changed, and what ran from there.

    Read from the recording's own epochs, which keep the holds their scans ran under: the hold
    log keeps what reverted attempts installed too.  None if a hold doesn't say what it was
    built from.
    """
    recording = plan.fork
    timeline: list[tuple[int, tuple[Any, ...]]] = []
    previous: tuple[tuple[Any, ...], ...] | None = None
    for epoch, first, _last in recording._causal_lineage.epochs_covering(
        plan.anchor_scan + 1, recording.state.scan_id
    ):
        holds = () if epoch.synthesis is None else tuple(epoch.synthesis.holds)
        rungs: list[Any] = []
        for hold in holds:
            built_from = getattr(hold, PILOT_RUNGS_BUILT_FROM, None)
            if built_from is None:
                return None
            rungs.extend(built_from)
        keys = tuple(_rung_identity(rung) for rung in rungs)
        if keys != previous:
            timeline.append((first, tuple(rungs)))
            previous = keys
    return timeline


def _installed(timeline: list[tuple[int, tuple[Any, ...]]]) -> list[Any]:
    """Each piece of temporary logic once, in the order it went in."""
    seen: set[tuple[Any, ...]] = set()
    rungs: list[Any] = []
    for _first, active in timeline:
        for rung in active:
            key = _rung_identity(rung)
            if key not in seen:
                seen.add(key)
                rungs.append(rung)
    return rungs


def _armed(fork: Any, avoid: Any) -> tuple[Any, ...]:
    """The avoid checks a drive's waits watch, armed where the plan starts as a seek arms them."""
    if avoid is None:
        return ()
    session = CoastSession(fork)
    session.arm_avoid(avoid)
    return session._avoid_triggers


def _ran_clear(fork: Any, scans: int, armed: tuple[Any, ...]) -> bool:
    """Run *scans* scans, as a seek does; False if a state to avoid came up on the way."""
    session = CoastSession(fork)
    session._avoid_triggers = armed
    receipt = session.seek((_NEVER,), budget=scans)
    return not receipt.avoided


def _drive(
    plan: Plan,
    timeline: list[tuple[int, tuple[Any, ...]]],
    change: _Change,
    goal: Callable[[Any], bool],
    avoid: Any,
    left: Callable[[], float],
) -> Any | None:
    """The plan run again from its anchor, with *change* made, or None if that doesn't reach."""
    recording = plan.fork
    tip = recording.state.scan_id
    snap = recording._scan_log.snapshot()
    patches = snap.patches_by_scan
    fork = fork_with_pilot_rungs(recording, (), scan_id=plan.anchor_scan, history_budget=math.inf)
    # A checkpoint records the forces whether or not they changed.  Only a change is a step.
    forces: dict[int, Mapping[str, Any]] = {}
    previous = repr(dict(fork._forces))
    for scan in sorted(snap.force_changes_by_scan):
        record = snap.force_changes_by_scan[scan]
        if scan <= plan.anchor_scan:
            fork._forces.clear()
            fork._forces.update(record)
        elif repr(record) != previous:
            forces[scan] = record
        previous = repr(record)
    # Everything applies before a scan: the logic its epoch ran, and the patch or force recorded
    # at it.  So each is a mark at the scan before.
    starts = {first - 1: rungs for first, rungs in timeline}
    marks = sorted(set(starts) | {scan - 1 for scan in patches} | {scan - 1 for scan in forces})
    armed: tuple[Any, ...] | None = None
    ran: tuple[tuple[Any, ...], ...] = ()
    kept: tuple[tuple[Any, ...], ...] = ()
    try:
        while fork.state.scan_id < tip:
            if left() <= 0:
                return None
            at = fork.state.scan_id
            sets: dict[str, Any] = {}
            if at in starts:
                rungs = starts[at]
                before = set(ran)
                ran = tuple(_rung_identity(rung) for rung in rungs)
                for rung, key in zip(rungs, ran, strict=True):
                    if key not in before and change.get(key) == "set":
                        sets[rung.dest] = rung.value
                active = tuple(rung for rung in rungs if _rung_identity(rung) not in change)
                keys = tuple(_rung_identity(rung) for rung in active)
                if keys != kept:
                    fork = fork_with_pilot_rungs(fork, active, history_budget=math.inf)
                    kept = keys
            patch = {**patches.get(at + 1, {}), **sets}
            if patch:
                fork.patch(patch)
            if at + 1 in forces:
                fork._forces.clear()
                fork._forces.update(forces[at + 1])
            until = min([mark for mark in marks if at < mark < tip] or [tip])
            if armed is None:
                armed = _armed(fork, avoid)
            if not _ran_clear(fork, until - at, armed) or fork.state.scan_id != until:
                return None
    except _AvoidCheckRaised:
        return None
    if not goal(dict(fork.state.tags)):
        return None
    return fork


def _without(rungs: tuple[Any, ...], change: _Change) -> tuple[Any, ...]:
    return tuple(rung for rung in rungs if _rung_identity(rung) not in change)


def _names_left(
    names: tuple[str, ...], gone: tuple[Any, ...], kept: tuple[Any, ...]
) -> tuple[str, ...]:
    dropped = {rung.dest for rung in gone} - {rung.dest for rung in kept}
    return tuple(name for name in names if name not in dropped)


def _journal(journal: tuple[PlanStep, ...], change: _Change) -> tuple[PlanStep, ...]:
    """The journal with the dropped logic gone and each Set where its logic went in."""
    steps: list[PlanStep] = []
    for step in journal:
        if not step.rungs:
            steps.append(step)
            continue
        kept = _without(step.rungs, change)
        gone = tuple(rung for rung in step.rungs if _rung_identity(rung) in change)
        if not gone:
            steps.append(step)
            continue
        if step.kind == "force":
            sets = tuple(
                (rung.dest, rung.value) for rung in gone if change[_rung_identity(rung)] == "set"
            )
            if sets:
                steps.append(
                    PlanStep(
                        kind="patch",
                        scan=step.scan,
                        scans=0,
                        inputs=sets,
                        label=", ".join(dict.fromkeys(tag for tag, _value in sets)),
                    )
                )
            if kept:
                inputs = tuple((rung.dest, rung.value) for rung in kept)
                steps.append(
                    replace(
                        step,
                        inputs=inputs,
                        label=", ".join(dict.fromkeys(tag for tag, _value in inputs)),
                        rungs=kept,
                    )
                )
            continue
        if step.kind == "revoke":
            if kept:
                inputs = tuple((rung.dest, rung.value) for rung in kept)
                steps.append(
                    replace(
                        step,
                        inputs=inputs,
                        label=", ".join(dict.fromkeys(tag for tag, _value in inputs)),
                        rungs=kept,
                    )
                )
            continue
        steps.append(
            replace(
                step,
                rungs=kept,
                steady_holds=_names_left(step.steady_holds, gone, kept),
                pulsing_holds=_names_left(step.pulsing_holds, gone, kept),
            )
        )
    return _joined_waits(steps)


def _bare_wait(step: PlanStep) -> bool:
    return (
        step.kind == "coast"
        and not step.rungs
        and not step.steady_holds
        and not step.pulsing_holds
        and not step.notes
    )


def _joined_waits(steps: list[PlanStep]) -> tuple[PlanStep, ...]:
    """Two waits left next to each other, once the logic installed between them is gone, as one."""
    joined: list[PlanStep] = []
    for step in steps:
        last = joined[-1] if joined else None
        if last is not None and _bare_wait(last) and _bare_wait(step) and not last.transition:
            joined[-1] = replace(
                step,
                scan=last.scan,
                scans=last.scans + step.scans,
                waiting_for=tuple(dict.fromkeys((*last.waiting_for, *step.waiting_for))),
            )
            continue
        joined.append(step)
    return tuple(joined)


def _hold_log(hold_log: tuple[Any, ...], change: _Change) -> tuple[Any, ...]:
    entries = []
    for entry in hold_log:
        kept = _without(entry.pilot_rungs, change)
        if kept:
            entries.append(replace(entry, pilot_rungs=kept))
    return tuple(entries)


def trim_holds(
    plan: Plan,
    goal: Callable[[Any], bool] | None,
    avoid: Any,
    settable: frozenset[str],
    clock: WallClock | None,
) -> Plan:
    """*plan* with the temporary logic it reaches without taken out, or *plan* as it was."""
    if not plan.reachable or plan.fork is None or goal is None:
        return plan
    timeline = _timeline(plan)
    installed = _installed(timeline or [])
    if timeline is None or not installed:
        return plan
    own = WallClock.starting_now(_WALL_S)

    def left() -> float:
        mine = _WALL_S if own is None else own.left()
        return mine if clock is None else min(mine, clock.left())

    change: dict[tuple[Any, ...], str] = {}
    fork = None
    for rung in installed:
        key = _rung_identity(rung)
        for what in ("drop", "set"):
            if what == "set" and rung.dest not in settable:
                continue
            if left() <= 0:
                break
            trial = {**change, key: what}
            tried = _drive(plan, timeline, trial, goal, avoid, left)
            if tried is not None:
                change, fork = trial, tried
                break
    if fork is None:
        return plan
    return replace(
        plan,
        fork=fork,
        journal=_journal(plan.journal, change),
        hold_log=_hold_log(plan.hold_log, change),
    )
