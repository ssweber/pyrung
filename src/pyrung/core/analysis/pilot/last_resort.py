"""Try one last move when how() has no reason left to try anything: a wait, or one input.

The pilot normally moves only for a reason that the trace gives it.  When it is about to
stop, it may instead run one move with no reason at all, on a fork.  Then how() is asked
again from where the move landed.  The pilot keeps the move only if that answer reaches the
goal.

A plan that needed this move says so, as a step of its own.  The answer is as real as any
other, because the scans ran.  But it also means that the trace missed something, and that
is worth knowing.  Each such plan names a gap in how the pilot reads a program.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, replace
from typing import Any

from pyrung.core.analysis.pilot.avoid import _avoid_scan_names, _AvoidCheckRaised
from pyrung.core.analysis.pilot.budget import WallClock
from pyrung.core.analysis.pilot.overlay import fork_with_pilot_rungs
from pyrung.core.analysis.pilot.physical import upstream_through_plant
from pyrung.core.scan_trace import SYSTEM_TAGS_BY_NAME
from pyrung.core.tag import TagType

# These stay within what is left of how()'s own limit.  One ask gets far less than the whole,
# because most moves fail.
_WALL_S = 5.0
_ASK_WALL_S = 2.0
_WAIT_SCANS = 40000
_FIRST_WAIT = 200
_VALUES_PER_TAG = 8
# A failing ask costs about as much as the search that stopped.  So far, a move that helps has
# been among the first few.  Each rescue seen so far took at most four asks.
_MAX_ASKS = 5


@dataclass(frozen=True)
class LastResort:
    """The one move a plan rests on, which nothing in the trace pointed to."""

    # The input that the move set, or None for a wait.
    tag: str | None
    value: Any
    # The scans that the move took before how() was asked again.
    scans: int
    # What how() said before the move.
    session_reason: str | None = None
    # The number of tries it took, including this one.
    tries: int = 1

    def step_line(self, number: int = 1) -> str:
        if self.tag is not None:
            move = f"{number}. Set {self.tag}={self.value!r} before the next scan."
        else:
            move = f"{number}. Wait {self.scans} {'scan' if self.scans == 1 else 'scans'}."
        reason = (self.session_reason or "No productive next action was found").removeprefix(
            "pilot: "
        )
        reason = reason.split("; still waiting on ", 1)[0]
        return (
            f"{move}\n   Nothing in the trace pointed to this. how() tried it because it was "
            f"about to stop: {reason.rstrip('.')}."
        )


def _input_values(setup: Any, name: str, current: Any) -> tuple[Any, ...]:
    tag = setup.work._tag_known_or_reached(name)
    if tag is None:
        return ()
    if tag.type is TagType.BOOL:
        return (not current,)
    domain = (setup.nd_domains or {}).get(name, ())
    return tuple(value for value in domain if value != current)[:_VALUES_PER_TAG]


def _inputs_to_try(stopped: Any, setup: Any, tags: Any) -> list[tuple[str, Any]]:
    """Return every value that an input able to reach the goal might take, in no ranked order."""
    goals = stopped.targets or ((stopped.target_tag, stopped.target_value),)
    if stopped.target_predicate is not None and not stopped.targets:
        upstream = frozenset(setup.steerable)
    else:
        harness = getattr(setup.work, "_harness", None)
        upstream = upstream_through_plant(setup.pdg, harness, (tag for tag, _ in goals))
    forced = setup.session_overrides
    moves: list[tuple[str, Any]] = []
    for name in sorted(frozenset(setup.steerable) & upstream - forced):
        moves.extend((name, value) for value in _input_values(setup, name, tags.get(name)))
    return moves


def _program_tags(state: Any) -> dict[str, Any]:
    return {name: value for name, value in state.tags.items() if name not in SYSTEM_TAGS_BY_NAME}


def _where(fork: Any) -> tuple[dict[str, Any], dict[str, Any]]:
    """Return the state that a wait can change, including a timer's fraction of a unit."""
    return _program_tags(fork.state), dict(fork.state.memory)


def _waited(
    fork: Any,
    goal: Callable[[Any], bool],
    avoid: Any,
    left: Callable[[], float],
    scans: int,
    bits: dict[str, bool] | None = None,
) -> bool:
    """Run *fork* until the goal holds, a bit leaves *bits*, or nothing is moving.

    Return False if the fork hit a state to avoid.
    """
    tripped = False

    def stop(state: Any) -> bool:
        nonlocal tripped
        tags = dict(state.tags)
        if _avoid_scan_names(avoid, tags):
            tripped = True
            return True
        if goal(tags):
            return True
        return bits is not None and any(tags.get(name) is not bit for name, bit in bits.items())

    start = fork.state.scan_id
    chunk = _FIRST_WAIT
    while fork.state.scan_id - start < scans and left() > 0:
        before = _where(fork)
        at = fork.state.scan_id
        fork.run_until(stop, max_cycles=min(chunk, scans - (at - start)))
        if tripped:
            return False
        if fork.state.scan_id - at < chunk or _where(fork) == before:
            break
        chunk *= 2
    return True


def try_moves(
    stopped: Any,
    setup: Any,
    goal: Callable[[Any], bool],
    avoid_pred: Any,
    ask: Callable[[Any, float], tuple[Any, bool]],
    max_scans: int,
) -> Any | None:
    """Return a plan that rests on one move from the session, or None if no move helps."""
    clock = WallClock.starting_now(_WALL_S)

    def left() -> float:
        return _WALL_S if clock is None else clock.left()

    start_scan = setup.anchor_scan + (1 if setup.waiting_overrides else 0)

    def start() -> Any:
        return fork_with_pilot_rungs(setup.work, (), scan_id=start_scan)

    tries = 0

    def asked(fork: Any, tag: str | None, value: Any) -> Any | None:
        nonlocal tries
        tries += 1
        plan, _cut_short = ask(fork, min(_ASK_WALL_S, left()))
        if not plan.reachable:
            return None
        move = LastResort(tag, value, fork.state.scan_id - start_scan, stopped.reason, tries)
        return replace(
            plan,
            anchor_scan=stopped.anchor_scan,
            session_inputs=stopped.session_inputs,
            waiting_overrides=stopped.waiting_overrides,
            last_resort=move,
        )

    idle = start()
    # The program stopped itself on the scan that applied the waiting overrides.  Any move from
    # there starts with a restart, and how() does not plan across a restart.
    if not idle._running:
        return None
    tags_at_start = idle.state.tags
    began = _where(idle)
    idle.step()
    idle_tags = _program_tags(idle.state)

    # how() has its answer here, so a check that raises on a scan of a try ends only that try.
    try:
        fork = start()
        # A wait's scans are charged a hundredth of a unit each, as the search's own scans are.
        wait = min(_WAIT_SCANS, max_scans * 100)
        bits = {name: value for name, value in idle_tags.items() if isinstance(value, bool)}
        # Ask where a bit first moves.  Otherwise a count that runs on would make the wait last
        # the whole limit.  Then ask where the state settles.  A wait that ends where an idle
        # scan did shows nothing new, so skip it.
        seen = [began, _where(idle)]
        for stop_at in (bits, None):
            scans = wait - (fork.state.scan_id - start_scan)
            if not _waited(fork, goal, avoid_pred, left, scans, stop_at):
                break
            where = _where(fork)
            if where in seen or left() <= 0:
                continue
            seen.append(where)
            plan = asked(fork, None, None)
            if plan is not None:
                return plan
    except _AvoidCheckRaised:
        pass

    def did_something(fork: Any, name: str) -> bool:
        tags = _program_tags(fork.state)
        return {**tags, name: None} != {**idle_tags, name: None}

    # One scan tells which moves did anything.  Those moves get their asks first.
    moved: list[tuple[str, Any, Any]] = []
    for name, value in _inputs_to_try(stopped, setup, tags_at_start):
        try:
            fork = start()
            fork.patch({name: value})
            fork.step()
            if not _avoid_scan_names(avoid_pred, dict(fork.state.tags)):
                moved.append((name, value, fork))
        except _AvoidCheckRaised:
            continue
    moved.sort(key=lambda move: not did_something(move[2], move[0]))

    for name, value, fork in moved[:_MAX_ASKS]:
        if left() <= 0:
            return None
        try:
            plan = asked(fork, name, value)
        except _AvoidCheckRaised:
            continue
        if plan is not None:
            return plan
    return None
