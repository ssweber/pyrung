"""What how() tries when it is about to stop: releasing forces, then restarting the program.

A session is a machine that already ran, with whatever its person forced, and a plan starts
from there.  When nothing works from there, the machine may only want what a tech would try
next: release the force, or STOP -> RUN.  Those leave the session, so they are never ordinary
steps.  They run on a fork, only when how() was about to stop, and a plan that needs one says
so as its first step.

A restart that falls short is still worth reporting.  Retentive tags keep their values through
STOP -> RUN, which is often why.  The scans decide which of them matter: each is written back
to its default while stopped, how() is asked again, and the smallest set found goes in the
report.  Writing tags by hand is advice to the person, never a step of a plan.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, replace
from typing import Any

from pyrung.core.analysis.pilot.avoid import _avoid_weighed_names, _AvoidCheckRaised
from pyrung.core.analysis.pilot.budget import WallClock
from pyrung.core.analysis.pilot.overlay import fork_with_pilot_rungs
from pyrung.core.analysis.query import _rung_label
from pyrung.core.scan_trace import SYSTEM_TAGS_BY_NAME

# Within what is left of how()'s own limit.  A plan from a restart is worth a search of its own;
# the report behind it is not, though the route programs need 13 s for theirs.
_RESTART_WALL_S = 30.0
_CLEAR_WALL_S = 20.0
_RELEASE_WALL_S = 20.0
_MAX_SURVIVORS = 12
_RUNGS_SHOWN = 4


def _values(survivors: Mapping[str, tuple[Any, Any]]) -> str:
    return ", ".join(f"{tag}={value!r}" for tag, (value, _) in survivors.items())


@dataclass(frozen=True)
class RestartReport:
    """What a restart did for a goal how() could not reach from the session."""

    reached: bool
    # Tag -> (the value it keeps through a restart, its default).  Inputs are left out, since
    # how() can set those itself.
    survivors: Mapping[str, tuple[Any, Any]]
    # The smallest set found that reaches the goal if written while stopped.
    clear: Mapping[str, Any] | None = None
    # Scans from the restart to the goal once they are cleared.
    clear_scans: int | None = None
    # Rungs that write each tag in *clear*, as a person would name them.
    written_by: Mapping[str, tuple[str, ...]] | None = None
    # Forces the restart releases.
    released_forces: tuple[str, ...] = ()
    # What how() said from the session, before the restart.
    session_reason: str | None = None
    # Avoid conditions the state after a restart trips, which rules a restart out.
    avoided: tuple[str, ...] = ()
    # The try ran out of time, so it says nothing about what a restart can do.
    no_answer: bool = False
    # An avoid check raised on a scan of the try, and what it raised.
    raised: str | None = None
    # Time ran out with survivors in *clear* still untried, so it may not be the smallest.
    clear_untried: bool = False

    def step_line(self, number: int = 1) -> str:
        """The first step of a plan that needed the restart."""
        line = f"{number}. Stop the program and go back to RUN."
        if self.released_forces:
            line += f" This releases the force on {', '.join(self.released_forces)}."
        reason = (self.session_reason or "No productive next action was found").removeprefix(
            "pilot: "
        )
        return f"{line}\n   how() found no way from where the session is: {reason.rstrip('.')}."

    def lines(self) -> list[str]:
        """The report as text, for a plan that stopped."""
        if self.avoided:
            return [
                f"Restart: not tried, the state after it is avoided ({', '.join(self.avoided)})."
            ]
        if self.no_answer:
            return ["Restart: how() ran out of time before it had an answer."]
        if self.raised is not None:
            return [f"Restart: no answer, on a scan after it avoid check {self.raised}."]
        if self.clear is None:
            kept = _values(self.survivors)
            verb = "survives" if len(self.survivors) == 1 else "survive"
            return [
                "Restart: stopping the program and going back to RUN does not reach it either"
                + (f" ({kept} {verb})." if kept else ".")
            ]
        needed = {tag: self.survivors[tag] for tag in self.clear}
        verb = "survives" if len(needed) == 1 else "survive"
        wanted = ", ".join(f"{tag}={value!r}" for tag, value in self.clear.items())
        scans = "scan" if self.clear_scans == 1 else "scans"
        lines = [
            f"Restart: alone it falls short, because {_values(needed)} {verb} it.",
            f"  With {wanted} written while stopped, a restart reaches it in "
            f"{self.clear_scans} {scans}.",
            "  Clear those by hand while stopped, or make them non-retentive.",
        ]
        if self.clear_untried:
            lines.append("  how() ran out of time checking, so not all of them may be needed.")
        for tag, rungs in (self.written_by or {}).items():
            shown = ", ".join(rungs[:_RUNGS_SHOWN])
            more = len(rungs) - _RUNGS_SHOWN
            lines.append(f"  {tag} is written by {shown}{f' and {more} more' if more > 0 else ''}.")
        return lines


@dataclass(frozen=True)
class ReleaseReport:
    """The forces a plan had to release, because nothing worked with them on."""

    forces: tuple[str, ...]
    # What how() said with them on.
    session_reason: str | None = None

    def step_line(self, number: int = 1) -> str:
        reason = (self.session_reason or "No productive next action was found").removeprefix(
            "pilot: "
        )
        return (
            f"{number}. Release the force on {', '.join(self.forces)}.\n"
            f"   how() found no way with it on: {reason.rstrip('.')}."
        )


def try_release(
    stopped: Any,
    plc: Any,
    ask: Callable[[Any, float], tuple[Any, bool]],
) -> tuple[Any | None, bool]:
    """A plan with the forces it must release off, and whether a search with all of them off ended.

    The plan is None if releasing them does not help.  The search having ended, with no plan,
    means it found nothing left to do rather than running out of time or work.

    Forces are the person's own, so as many as can stay on do.  how() has its answer here,
    so a check that raises on a scan of a try costs that try and not the answer.
    """
    forces = tuple(sorted(plc._forces))
    if not forces:
        return None, False
    clock = WallClock.starting_now(_RELEASE_WALL_S)

    def reaches(names: tuple[str, ...]) -> tuple[Any | None, bool]:
        wall_s = _RELEASE_WALL_S if clock is None else clock.left()
        fork = fork_with_pilot_rungs(plc, ())
        for name in names:
            fork.unforce(name)
        try:
            plan, cut_short = ask(fork, wall_s)
        except _AvoidCheckRaised:
            return None, False
        return (plan if plan.reachable else None), not cut_short

    plan, finished = reaches(forces)
    if plan is None:
        return None, finished
    needed = forces
    if len(forces) <= _MAX_SURVIVORS:
        for name in forces:
            if clock is not None and clock.left() <= 0:
                break
            fewer = tuple(other for other in needed if other != name)
            smaller, _finished = reaches(fewer)
            if smaller is not None:
                plan, needed = smaller, fewer
    return replace(plan, released=ReleaseReport(needed, stopped.reason)), finished


def _restarted(plc: Any, writes: Mapping[str, Any] | None = None) -> Any:
    """A fork of *plc* after STOP -> RUN, with *writes* made while stopped."""
    fork = fork_with_pilot_rungs(plc, (), keep_overrides=False)
    fork.stop()
    if writes:
        fork._state = fork._state.with_tags(dict(writes))
    fork.start()
    return fork


def _survivors(plc: Any, steerable: frozenset[str]) -> dict[str, tuple[Any, Any]]:
    """Program tags a restart leaves at something other than their default."""
    tags = plc.state.tags
    found: dict[str, tuple[Any, Any]] = {}
    for name, tag in sorted(plc._known_tags_by_name.items()):
        if name in SYSTEM_TAGS_BY_NAME or name in steerable or name not in tags:
            continue
        if tags[name] != tag.default:
            found[name] = (tags[name], tag.default)
    return found


def _writers(pdg: Any, tag: str) -> tuple[str, ...]:
    rungs = []
    for index in sorted(pdg.writers_of.get(tag, ())):
        node = pdg.rung_nodes[index]
        rungs.append(f"rung {_rung_label(node.subroutine, node.rung_index)}")
    return tuple(dict.fromkeys(rungs))


def try_restart(
    stopped: Any,
    plc: Any,
    setup: Any,
    avoid_pred: Any,
    ask: Callable[[Any, float], tuple[Any, bool]],
) -> Any:
    """The plan to return in place of *stopped*.

    ``ask(fork, wall_s)`` runs how() on a restarted fork, with no restart of its own, and
    returns its plan and whether it ran out of time or work.  how() already has an answer
    here, so a check that raises on a scan of a try costs that try and not the answer.
    """
    session_tags = dict(plc.state.tags)
    fork = _restarted(plc)
    start_tags = dict(fork.state.tags)
    if plc.state.scan_id == 0 and start_tags == session_tags:
        return stopped  # The session is already what a restart would give.

    # The state a restart lands on has run no scan, and a system tag is not in it yet, so a
    # check may have no answer for it; the scans judge it from there.  A check that does
    # answer, and says this state is one to avoid, rules a restart out.
    avoided = _avoid_weighed_names(avoid_pred, start_tags)
    if avoided:
        return replace(stopped, restart=RestartReport(reached=False, survivors={}, avoided=avoided))

    released = tuple(sorted(plc._forces))
    survivors = _survivors(fork, frozenset(setup.steerable))
    try:
        plan, cut_short = ask(fork, _RESTART_WALL_S)
    except _AvoidCheckRaised as raised:
        # how() has its answer.  This try has none, so the report says why.
        failed = f"{raised.check!r} raised {type(raised.error).__name__}: {raised.error}"
        return replace(stopped, restart=RestartReport(reached=False, survivors={}, raised=failed))
    if plan.reachable:
        report = RestartReport(
            reached=True,
            survivors=survivors,
            released_forces=released,
            session_reason=stopped.reason,
        )
        return replace(plan, restart=report)
    if cut_short:
        return replace(stopped, restart=RestartReport(reached=False, survivors={}, no_answer=True))

    clear, clear_scans, untried = _smallest_clear(plc, survivors, ask)
    report = RestartReport(
        reached=False,
        survivors=survivors,
        clear=clear,
        clear_scans=clear_scans,
        clear_untried=untried,
        written_by=(
            {tag: _writers(setup.pdg, tag) for tag in clear} if clear is not None else None
        ),
        released_forces=released,
        session_reason=stopped.reason,
    )
    return replace(stopped, restart=report)


def _smallest_clear(
    plc: Any,
    survivors: Mapping[str, tuple[Any, Any]],
    ask: Callable[[Any, float], tuple[Any, bool]],
) -> tuple[dict[str, Any] | None, int | None, bool]:
    """Which survivors must go back to their defaults for a restart to reach the goal.

    Returns them, the scans from the restart, and whether time ran out with some still untried.
    """
    if not survivors or len(survivors) > _MAX_SURVIVORS:
        return None, None, False
    clock = WallClock.starting_now(_CLEAR_WALL_S)

    def reaches(writes: dict[str, Any]) -> tuple[int | None, bool]:
        wall_s = _CLEAR_WALL_S if clock is None else clock.left()
        # These writes are advice, and how() has its answer already.  A check that raises on
        # a scan of this try is no reason to lose that answer: the try just has no plan.
        try:
            plan, cut_short = ask(_restarted(plc, writes), wall_s)
        except _AvoidCheckRaised:
            return None, False
        return (plan.total_scans if plan.reachable else None), cut_short

    clear = {tag: default for tag, (_, default) in survivors.items()}
    scans, _cut_short = reaches(clear)
    if scans is None:
        return None, None, False
    # Whatever can be left out, is.  Each answer here came from scans, and a search that ran
    # out of time said nothing about the tag it left in.
    for tag in list(clear):
        without = {name: value for name, value in clear.items() if name != tag}
        if not without:
            break
        fewer, cut_short = reaches(without)
        if cut_short:
            return clear, scans, True
        if fewer is not None:
            clear, scans = without, fewer
    return clear, scans, False
