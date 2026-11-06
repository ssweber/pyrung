"""Temporary logic a reached plan turns out not to need, found by running the plan without it.

The pilot installs temporary logic when the trace says an input has to stay where it is through
a wait.  Often nothing would have moved it: a plain Set holds as well, or the program holds it
itself.  The trace can't always tell, but running the plan can.  So, once a plan has reached,
each piece of temporary logic is tried in the order it went in: the plan is run again from its
anchor without it, and then with a Set of its value in its place.  The simpler plan is kept
only if it still reaches the goal on the same scan, with nothing to avoid on the way: each wait
watches the avoid checks as the drive's own seeks do, and every check is judged again where each
wait ends.  A check that raises on a try's state ends that try, not how(): that state is not the
plan's, and the plan the drive found stands.

A release (logic holding an input False) is tried as a Set first, so a command the plan lets go
of stays let go of.  A note on the relation an install satisfies goes with its Set: the relation
is the requirement, the value an example.  An install of several rungs with notes stays, since
its notes don't say which is whose.

The text leaves out a patch of an input logic holds.  So logic over an input the recording
patches to its value where the logic goes in is only ever that Set, and logic over an input it
patches on another scan the logic is in stays: without it, the replay would make a patch the
text doesn't name.

The kept plan is that new run, so what it says and what it replays are one recording.
"""

from __future__ import annotations

import math
from collections.abc import Callable, Mapping
from dataclasses import replace
from typing import Any

from pyrung.core.analysis.graph import Plan, PlanStep
from pyrung.core.analysis.pilot.avoid import _avoid_scan_names, _AvoidCheckRaised
from pyrung.core.analysis.pilot.budget import (
    SearchOutOfMemory,
    SearchOutOfTime,
    WallClock,
    search_limits,
)
from pyrung.core.analysis.pilot.coast import CoastSession, CoastTrigger
from pyrung.core.analysis.pilot.overlay import PILOT_RUNGS_BUILT_FROM, fork_with_pilot_rungs
from pyrung.core.analysis.pilot.world_key import _rung_identity
from pyrung.core.coast_run import memo_of

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


def _left(clock: WallClock | None) -> float:
    """Seconds a try has left; no clock (the wall clock turned off) is no bound."""
    return math.inf if clock is None else clock.left()


def _ran_clear(fork: Any, scans: int, avoid: Any, limits: Callable[[], None]) -> bool:
    """Run *scans* scans, as a seek does; False if a state to avoid came up on the way.

    The avoid checks are armed where the wait starts, as a seek arms them, and every one is
    judged again where it ends.  The try's own clock bounds the wait.
    """
    session = CoastSession(fork)
    if avoid is not None:
        session.arm_avoid(avoid)
    memo = memo_of(fork)
    memo.check_limits = limits
    try:
        receipt = session.seek((_NEVER,), budget=scans)
    finally:
        memo.check_limits = None
    return not receipt.avoided and not _avoid_scan_names(avoid, dict(fork.state.tags))


def _drive(
    plan: Plan,
    timeline: list[tuple[int, tuple[Any, ...]]],
    change: _Change,
    goal: Callable[[Any], bool],
    avoid: Any,
    clock: WallClock | None,
) -> Any | None:
    """The plan run again from its anchor, with *change* made, or None if that doesn't reach."""
    recording = plan.fork
    tip = recording.state.scan_id
    snap = recording._scan_log.snapshot()
    patches = snap.patches_by_scan
    fork = fork_with_pilot_rungs(recording, (), scan_id=plan.anchor_scan)
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
    limits = search_limits(clock)
    # A Set goes in where its logic first did, once, as the plan's text has it.
    set_already: set[tuple[Any, ...]] = set()
    kept: tuple[tuple[Any, ...], ...] = ()
    try:
        while fork.state.scan_id < tip:
            if _left(clock) <= 0:
                return None
            at = fork.state.scan_id
            sets: dict[str, Any] = {}
            if at in starts:
                rungs = starts[at]
                for rung in rungs:
                    key = _rung_identity(rung)
                    if change.get(key) == "set" and key not in set_already:
                        set_already.add(key)
                        sets[rung.dest] = rung.value
                active = tuple(rung for rung in rungs if _rung_identity(rung) not in change)
                keys = tuple(_rung_identity(rung) for rung in active)
                if keys != kept:
                    fork = fork_with_pilot_rungs(fork, active)
                    kept = keys
            patch = {**patches.get(at + 1, {}), **sets}
            if patch:
                fork.patch(patch)
            if at + 1 in forces:
                fork._forces.clear()
                fork._forces.update(forces[at + 1])
            until = min([mark for mark in marks if at < mark < tip] or [tip])
            if not _ran_clear(fork, until - at, avoid, limits) or fork.state.scan_id != until:
                return None
    except (_AvoidCheckRaised, SearchOutOfTime, SearchOutOfMemory):
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
    # Where in *steps* a step taken out of the plan left two waits next to each other.
    joins: set[int] = set()
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
                        # Only a step of one rung is trimmed with notes on: they are its own.
                        notes=step.notes,
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
            elif not sets:
                joins.add(len(steps))
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
            else:
                joins.add(len(steps))
            continue
        steps.append(
            replace(
                step,
                rungs=kept,
                steady_holds=_names_left(step.steady_holds, gone, kept),
                pulsing_holds=_names_left(step.pulsing_holds, gone, kept),
            )
        )
    return _joined_waits(steps, joins)


def _bare_wait(step: PlanStep) -> bool:
    return (
        step.kind == "coast"
        and not step.rungs
        and not step.steady_holds
        and not step.pulsing_holds
        and not step.notes
    )


def _joined_waits(steps: list[PlanStep], joins: set[int]) -> tuple[PlanStep, ...]:
    """Two waits left next to each other, once the logic installed between them is gone, as one."""
    joined: list[PlanStep] = []
    for index, step in enumerate(steps):
        last = joined[-1] if joined else None
        if (
            index in joins
            and last is not None
            and _bare_wait(last)
            and _bare_wait(step)
            and not last.transition
        ):
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


def _patches_under(
    plan: Plan, timeline: list[tuple[int, tuple[Any, ...]]], rung: Any
) -> tuple[bool, bool]:
    """Whether the recording patches *rung*'s input where it goes in, and while it is in.

    The plan's text leaves out a patch of an input logic holds, so taking the logic out leaves
    a patch the text doesn't name: one where it went in, to its own value, is its Set.
    """
    key = _rung_identity(rung)
    patches = plan.fork._scan_log.snapshot().patches_by_scan
    tip = plan.fork.state.scan_id
    ends = [first for first, _rungs in timeline[1:]] + [tip + 1]
    installs: list[int] = []
    scans: list[range] = []
    for (first, rungs), end in zip(timeline, ends, strict=True):
        if any(_rung_identity(other) == key for other in rungs):
            if not scans or scans[-1].stop != first:
                installs.append(first)
            scans.append(range(first, end))
    at_install = elsewhere = False
    for scan, patch in patches.items():
        if rung.dest not in patch or not any(scan in active for active in scans):
            continue
        if scan == installs[0] and patch[rung.dest] == rung.value:
            at_install = True
        else:
            elsewhere = True
    return at_install, elsewhere


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
    own = WallClock.starting_now(_WALL_S if clock is None else min(_WALL_S, clock.left()))
    # A note says what relation the logic satisfies.  An install of one rung hands its notes to
    # the Set in its place; one of several can't say which note is whose, so its logic stays.
    noted = {
        _rung_identity(rung)
        for step in plan.journal
        if step.kind == "force" and step.notes and len(step.rungs) > 1
        for rung in step.rungs
    }

    change: dict[tuple[Any, ...], str] = {}
    fork = None
    for rung in installed:
        key = _rung_identity(rung)
        if key in noted:
            continue
        at_install, elsewhere = _patches_under(plan, timeline, rung)
        if elsewhere:
            continue
        # A release is a Set of False first: dropping it leaves a command where it was pressed.
        tries = ("set", "drop") if rung.value is False else ("drop", "set")
        # Patched to its value where it went in, its logic dropped is that Set.
        for what in ("set",) if at_install else tries:
            if what == "set" and rung.dest not in settable:
                continue
            if _left(own) <= 0:
                break
            trial = {**change, key: what}
            tried = _drive(plan, timeline, trial, goal, avoid, own)
            if tried is not None:
                change, fork = trial, tried
                break
    if fork is None:
        return plan
    # The caller gets the fork to run, with no memo of the tries'.
    fork._coast_memo = None
    return replace(
        plan,
        fork=fork,
        journal=_journal(plan.journal, change),
        hold_log=_hold_log(plan.hold_log, change),
    )
