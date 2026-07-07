"""compass.py — the bearing layer of PILOT.

The compass is the generalized transition graph that PILOT navigates by. It is a
*bearing*, not a route: it keeps pointing at the target while the pilot is free
to detour. See ``pilot/CLAUDE.md``.

Edges carry two generalized axes:

* **driver** — *how the edge fires*: an ``Action`` ``(tag, value)`` to pulse, a
  ``WaitCause`` (let-run — hold state and let scans coast), or a request to be
  re-entered into the trace.
* **provenance** — *how the edge was learned*: a static route read by ``trace``,
  a runtime observation from ``sandbox``, or a learned transition.

One :class:`Compass` object holds them all: the static per-register value graph
(``CompassGraph``) plus the learned transition table (formerly ``InfluenceMap``).
"""

from __future__ import annotations

import logging
from collections import deque
from collections.abc import Iterable
from dataclasses import dataclass, replace
from enum import Enum
from typing import Any, Literal, TypeGuard

from pyrung.core.analysis.pilot.evidence import TransitionRoute
from pyrung.core.analysis.pilot.statics import (
    ANY_FROM,
    Action,
    ActionPair,
    CompassEdge,
    CompassGraph,
    CompassPlan,
    PipelineSlice,
    best_compass_plan,
    build_compass_graphs,
    detect_opaque_loop,
    detect_opaque_pipelines,
)
from pyrung.core.analysis.sp_values import _values_match

logger = logging.getLogger(__name__)

__all__ = [
    "ANY_FROM",
    "WAIT",
    "Action",
    "ActionPair",
    "Compass",
    "CompassEdge",
    "CompassEntry",
    "CompassGraph",
    "CompassObservation",
    "CompassPlan",
    "PipelineSlice",
    "Provenance",
    "TransitionCause",
    "WaitCause",
    "best_compass_plan",
    "build_compass_graphs",
    "detect_opaque_loop",
    "detect_opaque_pipelines",
    "is_action",
    "is_composite_action",
]


# ===========================================================================
# Drivers — how an edge fires
# ===========================================================================


@dataclass(frozen=True)
class WaitCause:
    """Let-run driver: a transition caused by time passing, not a tag write."""

    def __repr__(self) -> str:
        return "WAIT"


WAIT = WaitCause()
TransitionCause = Action | WaitCause


def is_action(cause: TransitionCause) -> TypeGuard[Action]:
    return isinstance(cause, tuple)


@dataclass(frozen=True)
class CompassObservation:
    """One instrument observation, deferred to the loop's RECORD point.

    Instruments (steer's try-verify wrappers, the skiff) *observe*; only RECORD
    applies observations to the compass — unconditionally, before ASSESS can
    revert the world, so negative knowledge (probe marks, contradictions)
    commits even when the trial is rejected.

    ``kind`` selects the write:

    - ``"edge"``       → :meth:`Compass.record` — a learned transition
    - ``"no_change"``  → :meth:`Compass.record_no_change` — probe mark only
    - ``"contradict"`` → :meth:`Compass.contradict` — falsify + probe mark
    """

    kind: Literal["edge", "no_change", "contradict"]
    tag: str
    cause: TransitionCause
    from_val: Any
    to_val: Any = None


# ===========================================================================
# One entry per (tag, from_val, cause) — provenance is the lifecycle
# ===========================================================================


class Provenance(Enum):
    """How a compass entry was learned — its place in the edge lifecycle.

    Replaces the old parallel ``_transitions`` / ``_probed`` dicts: an entry's
    *provenance* is what used to be smeared across the two structures.  The
    three **live** provenances (SEEDED / OBSERVED / CONFIRMED) carry a
    ``to_val`` and are the edges ``find_path`` walks; the two **tombstones**
    (NO_CHANGE / CONTRADICTED) have no destination and traversal skips them —
    but every entry, live or tombstone, still *is a probe mark*.  That is the
    invariant the anchor fact protects: the skiff's singles→pairs escalation
    reads the entry key set, never the provenance, so a demoted edge still
    terminates the escalation exactly as the old probe mark did.
    """

    SEEDED = "seeded"  # statically-seeded route (seed_routes); unconfirmed
    OBSERVED = "observed"  # a runtime motion recorded at RECORD
    CONFIRMED = "confirmed"  # (commit 2) minted only by outcome.py's factory
    NO_CHANGE = "no_change"  # probe mark: the cause was tried and nothing moved
    CONTRADICTED = "contradicted"  # a falsified edge, kept as negative knowledge


# Live (traversable) provenances — the edges find_path/off_path/transition_dest
# walk.  A CONTRADICTED or NO_CHANGE entry is a tombstone: still a probe mark,
# never a destination.
_LIVE_PROVENANCE = frozenset({Provenance.SEEDED, Provenance.OBSERVED, Provenance.CONFIRMED})


@dataclass(frozen=True)
class CompassEntry:
    """One learned transition (or probe mark) for a ``(tag, from_val, cause)``.

    Unifies the former dual-dict lifecycle: ``_transitions`` held the edge's
    destination, ``_probed`` held the fact it had been tried; here both live in
    one entry, distinguished by ``provenance``.  A live entry
    (``provenance in _LIVE_PROVENANCE``) has a real ``to_val``; a tombstone
    (NO_CHANGE / CONTRADICTED) has ``to_val=None`` and is skipped by traversal
    yet still counts as a probe mark.  ``contradict`` *demotes* a live edge to
    a CONTRADICTED tombstone rather than deleting it — a falsified seeded edge
    is negative knowledge, not a blank.
    """

    tag: str
    from_val: Any
    cause: TransitionCause
    to_val: Any
    provenance: Provenance

    @property
    def is_live(self) -> bool:
        return self.provenance in _LIVE_PROVENANCE


def is_composite_action(cause: Any) -> bool:
    """A skiff-learned *joint* cause: a tuple of action pairs that must fire in
    one window (``((tag, val), (tag, val))``), as opposed to a single
    ``(tag, val)`` action.  Both satisfy :func:`is_action`; this distinguishes
    the shape so consumers can propose the members as one batch."""
    return (
        isinstance(cause, tuple)
        and len(cause) > 0
        and all(isinstance(m, tuple) and len(m) == 2 and isinstance(m[0], str) for m in cause)
        and not isinstance(cause[0], str)
    )


# ===========================================================================
# The unified graph — static value graph + learned transitions in one object
# (absorbs the former InfluenceMap)
# ===========================================================================


class Compass:
    """One transition graph for a program, navigated by PILOT.

    Holds the static per-register value graph (``CompassGraph`` per role) and a
    learned transition table seeded at startup from static routes and extended
    by runtime observations.  An ``Action`` cause is a ``(tag, value)`` pair; a
    ``WaitCause`` is a let-run (time-passing) transition.
    """

    def __init__(self, slices: list[PipelineSlice] | None = None) -> None:
        self._slices: list[PipelineSlice] = list(slices or [])
        self._action_tags: frozenset[str] = (
            frozenset().union(*(s.action_tags for s in self._slices))
            if self._slices
            else frozenset()
        )
        # One entry per (tag, (from_val, cause)).  A live entry is a learned
        # edge; a tombstone (NO_CHANGE / CONTRADICTED) is a probe mark with no
        # destination.  Replaces the former parallel _transitions / _probed.
        self._entries: dict[str, dict[tuple[Any, TransitionCause], CompassEntry]] = {}
        self._graphs: tuple[CompassGraph, ...] = ()

    # -- static value-graph side --------------------------------------------

    @property
    def graphs(self) -> tuple[CompassGraph, ...]:
        return self._graphs

    def set_graphs(self, graphs: tuple[CompassGraph, ...]) -> None:
        self._graphs = graphs

    def best_plan(
        self,
        needed_tag: str,
        needed_value: Any,
        snapshot: dict[str, Any],
    ) -> CompassPlan | None:
        """Best static value-graph path for a need, if any."""
        return best_compass_plan(needed_tag, needed_value, snapshot, self._graphs)

    def seed_routes(self, target_tag: str, routes: list[TransitionRoute]) -> int:
        """Seed learned transitions from statically-known routes.

        Only seeds routes where both a source constraint on *target_tag*
        (giving the ``from_val``) and a steerable action tag in the enablers
        (giving the ``cause``) are known.  Returns the number of entries seeded.
        """
        seeded = 0
        for route in routes:
            if route.destination_value is None:
                continue
            from_val: Any = None
            for tag, value in route.source_constraints:
                if tag == target_tag:
                    from_val = value
                    break
            if from_val is None:
                continue
            for action_tag in sorted(route.action_tags):
                for tag, value in route.enablers:
                    if tag == action_tag:
                        self.record(
                            target_tag,
                            (action_tag, value),
                            from_val,
                            route.destination_value,
                            provenance=Provenance.SEEDED,
                        )
                        seeded += 1
                        break
        if seeded:
            logger.info("compass: seeded %d transitions for %s", seeded, target_tag)
        return seeded

    # -- learned transition side (was InfluenceMap) -------------------------

    @property
    def action_tags(self) -> frozenset[str]:
        return self._action_tags

    def has_transitions(self, tag: str) -> bool:
        # True iff a real edge was ever recorded for *tag* (a CONTRADICTED
        # tombstone still counts — it *was* an edge), matching the old
        # ``tag in self._transitions`` (which stayed True after contradict
        # emptied the per-tag dict).  A tag carrying only NO_CHANGE probe marks
        # never had a transition, so it reads False as it did before.
        return any(
            entry.provenance is not Provenance.NO_CHANGE
            for entry in self._entries.get(tag, {}).values()
        )

    def record(
        self,
        tag: str,
        cause: TransitionCause,
        from_val: Any,
        to_val: Any,
        provenance: Provenance = Provenance.OBSERVED,
    ) -> None:
        # A record always writes a live edge, overwriting whatever was at the
        # key — including reviving a CONTRADICTED tombstone if the edge is
        # learned again (old behavior: ``_transitions[key] = to_val``).  The
        # entry is its own probe mark, so no separate ``_probed`` write.
        table = self._entries.setdefault(tag, {})
        table[(from_val, cause)] = CompassEntry(tag, from_val, cause, to_val, provenance)

    def record_no_change(self, tag: str, cause: TransitionCause, from_val: Any) -> None:
        # Probe mark only: leave any existing entry (a live edge must NOT be
        # demoted by a no-change probe — old ``record_no_change`` never touched
        # ``_transitions``); create a NO_CHANGE tombstone only where nothing
        # was tried before.
        table = self._entries.setdefault(tag, {})
        key = (from_val, cause)
        if key not in table:
            table[key] = CompassEntry(tag, from_val, cause, None, Provenance.NO_CHANGE)

    def apply(self, observations: Iterable[CompassObservation]) -> None:
        """The RECORD write path: fold instrument observations into the compass.

        Instruments never call ``record``/``contradict`` themselves — they
        return :class:`CompassObservation` values and the loop applies them
        here, once per attempt / skiff round.
        """
        for obs in observations:
            if obs.kind == "edge":
                self.record(obs.tag, obs.cause, obs.from_val, obs.to_val)
            elif obs.kind == "contradict":
                self.contradict(obs.tag, obs.cause, obs.from_val)
            else:
                self.record_no_change(obs.tag, obs.cause, obs.from_val)

    def contradict(self, tag: str, cause: TransitionCause, from_val: Any) -> bool:
        """Live evidence falsified a learned edge — remove it.

        A statically-seeded route (``seed_routes``) records the writer's edge
        without its unreadable enablers; when the live trial applies *cause*
        from *from_val* and the register does NOT reach the recorded
        destination, the entry is a disproven hypothesis and must not keep
        shadowing genuine (skiff-learned) edges in ``find_path``.  The edge is
        *demoted* to a CONTRADICTED tombstone rather than deleted — negative
        knowledge, not a blank — and stays a probe mark (the cause was genuinely
        tried).  Returns True if a live edge was demoted.
        """
        table = self._entries.setdefault(tag, {})
        removed = False
        for key, entry in list(table.items()):
            if key[1] == cause and _values_match(key[0], from_val) and entry.is_live:
                table[key] = replace(entry, to_val=None, provenance=Provenance.CONTRADICTED)
                removed = True
        # Ensure the passed key carries a probe mark.  When it collapses onto a
        # just-demoted edge (bool/int keys share a dict slot) it is already a
        # tombstone; otherwise record a bare NO_CHANGE probe.
        pkey = (from_val, cause)
        if pkey not in table:
            table[pkey] = CompassEntry(tag, from_val, cause, None, Provenance.NO_CHANGE)
        return removed

    def find_path(
        self,
        tag: str,
        from_val: Any,
        to_val: Any,
    ) -> list[TransitionCause] | None:
        """BFS shortest transition-cause sequence through the learned table.

        Traverses only **live** edges — CONTRADICTED and NO_CHANGE tombstones
        are skipped, so a falsified edge never shadows a genuine path (they used
        to be deleted; a tombstone that still matched would change behavior).
        """
        live = self._live_edges(tag)
        if not live:
            return None
        if _values_match(from_val, to_val):
            return []

        queue: deque[tuple[Any, list[TransitionCause]]] = deque([(from_val, [])])
        visited: set[Any] = {from_val}

        while queue:
            state, path = queue.popleft()
            for (s, cause), dest in live.items():
                if not _values_match(s, state):
                    continue
                if dest in visited:
                    continue
                new_path = [*path, cause]
                if _values_match(dest, to_val):
                    return new_path
                visited.add(dest)
                queue.append((dest, new_path))

        return None

    def _live_edges(self, tag: str) -> dict[tuple[Any, TransitionCause], Any]:
        """The traversable ``(from_val, cause) -> to_val`` edges for *tag*.

        The live subset of the entry table — the old ``_transitions[tag]``,
        which only ever held live edges (record added, contradict deleted).
        """
        return {
            key: entry.to_val for key, entry in self._entries.get(tag, {}).items() if entry.is_live
        }

    def unprobed_actions(
        self,
        tag: str,
        from_val: Any,
        available_actions: set[Action] | frozenset[Action],
    ) -> list[Action]:
        """Available actions not yet tried from *from_val* for *tag*."""
        return sorted(available_actions - self.probed_actions(tag, from_val))

    def probed_actions(self, tag: str, from_val: Any) -> set[Action]:
        """Actions already probed from *from_val* for *tag*.

        Every entry key — live edge or tombstone — is a probe mark, so this
        reads the whole entry table (the old ``_probed`` set was exactly the
        union of every write's key).
        """
        return {
            cause
            for (fv, cause) in self._entries.get(tag, {})
            if fv == from_val and is_action(cause)
        }

    def transition_dest(
        self,
        tag: str,
        from_val: Any,
        cause: TransitionCause,
    ) -> Any | None:
        """Observed destination for one transition cause from *from_val*."""
        for (fv, candidate_cause), dest in self._live_edges(tag).items():
            if candidate_cause == cause and _values_match(fv, from_val):
                return dest
        return None

    def off_path_actions(self, tag: str, from_val: Any, to_val: Any) -> set[Action]:
        """Actions known to move *tag* away from the BFS path toward *to_val*.

        Once we know the shortest path, any action from the current state
        that goes to a state NOT on that path (or with no path to the
        target) is off-path and should be tried after path actions.
        """
        path = self.find_path(tag, from_val, to_val)
        if not path:
            return set()
        good_cause = path[0]
        table = self._live_edges(tag)

        # Compute states on the BFS path
        on_path: set[Any] = {from_val}
        state = from_val
        for cause in path:
            dest = table.get((state, cause))
            if dest is not None:
                on_path.add(dest)
                state = dest

        off_path: set[Action] = set()
        for (fv, cause), dest in table.items():
            if not _values_match(fv, from_val):
                continue
            if cause == good_cause or not is_action(cause):
                continue
            if dest not in on_path:
                off_path.add(cause)
        return off_path
