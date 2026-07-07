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
from dataclasses import dataclass
from enum import Enum
from typing import Any, Literal, TypeGuard

from pyrsistent import PMap, PRecord, field, pmap

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
    CONFIRMED = "confirmed"  # minted only by outcome.confirmed_entry (verify)
    NO_CHANGE = "no_change"  # probe mark: the cause was tried and nothing moved
    CONTRADICTED = "contradicted"  # a falsified edge, kept as negative knowledge


# Live (traversable) provenances — the edges find_path/off_path/transition_dest
# walk.  A CONTRADICTED or NO_CHANGE entry is a tombstone: still a probe mark,
# never a destination.
_LIVE_PROVENANCE = frozenset({Provenance.SEEDED, Provenance.OBSERVED, Provenance.CONFIRMED})


def _canon(value: Any) -> Any:
    """Canonicalize a keyed value at RECORD: ``bool`` → ``int``.

    ``hash(True) == hash(1)`` and ``True == 1``, so a PMap already collapses the
    two forms into one slot; canonicalizing makes the stored key uniform so a
    later exact ``==`` read (``probed_actions``) never depends on which form was
    written.  Genuine value fuzz (graph BFS, ``ANY_FROM``) stays in
    ``_values_match`` — this only normalizes the bool/int duplicate.
    """
    return int(value) if isinstance(value, bool) else value


class CompassEntry(PRecord):
    """One learned transition (or probe mark) for a ``(tag, from_val, cause)``.

    A ``pyrsistent`` PRecord: the entry table is a persistent value, so learned
    knowledge is shared structurally and never mutated out from under a holder
    (the anchor fact — *knowledge commits, the world reverts*).  Unifies the
    former dual-dict lifecycle: ``_transitions`` held the destination, ``_probed``
    held the fact it had been tried; here both live in one entry, distinguished
    by ``provenance``.  A live entry (``provenance in _LIVE_PROVENANCE``) has a
    real ``to_val``; a tombstone (NO_CHANGE / CONTRADICTED) has ``to_val=None``,
    is skipped by traversal, yet still counts as a probe mark.  ``contradict``
    *demotes* a live edge to a CONTRADICTED tombstone rather than deleting it.

    A ``CONFIRMED`` entry is minted **only** by ``outcome.confirmed_entry`` — the
    general write path (:meth:`Compass.record`) rejects that provenance, so
    "verify is the sole source of CONFIRMED" is structural, not convention.
    """

    tag = field(type=str)
    from_val = field()
    cause = field()
    to_val = field()
    provenance = field(type=Provenance)

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
# Pure table operations — every write is persistent-table-in, persistent-
# table-out; the Compass methods and the RECORD fold both delegate here so
# the semantics exist exactly once.
# ===========================================================================


def _table_record(
    entries: PMap,
    tag: str,
    cause: TransitionCause,
    from_val: Any,
    to_val: Any,
    provenance: Provenance,
) -> tuple[PMap, bool]:
    """Write a live edge, overwriting whatever was at the key.

    Including reviving a CONTRADICTED tombstone if the edge is learned again
    (old behavior: ``_transitions[key] = to_val``).  The entry is its own probe
    mark, so there is no separate ``_probed`` write.

    Returns ``(next_table, changed)`` — ``changed`` is False (and the table is
    returned untouched) when the key already carries an identical entry, so a
    re-learned edge does not count as new knowledge.

    CONFIRMED is off-limits here: it is minted only by
    ``outcome.confirmed_entry`` (verify is the sole source), so the general
    write path structurally cannot forge it.
    """
    if provenance is Provenance.CONFIRMED:
        raise ValueError(
            "CONFIRMED entries must come from outcome.confirmed_entry(); record() cannot mint them"
        )
    fv = _canon(from_val)
    key = (tag, fv, cause)
    entry = CompassEntry(tag=tag, from_val=fv, cause=cause, to_val=to_val, provenance=provenance)
    if entries.get(key) == entry:
        return entries, False
    return entries.set(key, entry), True


def _table_no_change(
    entries: PMap, tag: str, cause: TransitionCause, from_val: Any
) -> tuple[PMap, bool]:
    """Probe mark only.

    Leaves any existing entry alone (a live edge must NOT be demoted by a
    no-change probe — the old ``record_no_change`` never touched
    ``_transitions``); creates a NO_CHANGE tombstone only where nothing was
    tried before.  Returns ``(next_table, changed)`` — ``changed`` is True only
    when a fresh probe mark was added.
    """
    fv = _canon(from_val)
    key = (tag, fv, cause)
    if key in entries:
        return entries, False
    return (
        entries.set(
            key,
            CompassEntry(
                tag=tag, from_val=fv, cause=cause, to_val=None, provenance=Provenance.NO_CHANGE
            ),
        ),
        True,
    )


def _table_contradict(
    entries: PMap, tag: str, cause: TransitionCause, from_val: Any
) -> tuple[PMap, bool, bool]:
    """Demote every matching live edge to a CONTRADICTED tombstone.

    The evolver advances the persistent table and its ``.persistent()`` is the
    next value.  Returns ``(next_table, changed, demoted_any)`` — ``changed`` is
    True when a live edge was demoted *or* a fresh probe mark was added;
    ``demoted_any`` (the historic second element) is True only for a live-edge
    demotion, which is what the public ``contradict`` reports.
    """
    evolver = entries.evolver()
    removed = False
    for key, entry in entries.items():
        if key[0] == tag and key[2] == cause and _values_match(key[1], from_val) and entry.is_live:
            evolver[key] = entry.set(to_val=None, provenance=Provenance.CONTRADICTED)
            removed = True
    # Ensure the passed key carries a probe mark.  When it collapses onto a
    # just-demoted edge (bool/int keys share a PMap slot) it is already a
    # tombstone; otherwise record a bare NO_CHANGE probe.
    pkey = (tag, _canon(from_val), cause)
    probe_added = False
    if pkey not in entries:
        evolver[pkey] = CompassEntry(
            tag=tag,
            from_val=_canon(from_val),
            cause=cause,
            to_val=None,
            provenance=Provenance.NO_CHANGE,
        )
        probe_added = True
    return evolver.persistent(), (removed or probe_added), removed


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
        # One entry per (tag, from_val, cause), a persistent PMap of PRecords
        # advanced by the RECORD write path.  A live entry is a learned edge; a
        # tombstone (NO_CHANGE / CONTRADICTED) is a probe mark with no
        # destination.  Replaces the former parallel _transitions / _probed.
        #
        # NOT a perf lever: the table is tiny and off the hot path — the PMap is
        # here for the *value* semantics (knowledge is a shared persistent value
        # the world's revert never touches), not for speed.  Do not "optimize"
        # it back to a plain dict.
        self._entries: PMap = pmap()
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

    def _tag_entries(self, tag: str) -> Iterable[tuple[Any, TransitionCause, CompassEntry]]:
        """The ``(from_val, cause, entry)`` triples recorded for *tag*.

        The entry table is a flat PMap keyed by ``(tag, from_val, cause)``; this
        projects out one tag.  Tables are tiny, so the scan is free — see the
        "NOT a perf lever" note on ``_entries``.
        """
        for (t, fv, cause), entry in self._entries.items():
            if t == tag:
                yield fv, cause, entry

    def has_transitions(self, tag: str) -> bool:
        # True iff a real edge was ever recorded for *tag* (a CONTRADICTED
        # tombstone still counts — it *was* an edge), matching the old
        # ``tag in self._transitions`` (which stayed True after contradict
        # emptied the per-tag dict).  A tag carrying only NO_CHANGE probe marks
        # never had a transition, so it reads False as it did before.
        return any(
            entry.provenance is not Provenance.NO_CHANGE
            for _fv, _cause, entry in self._tag_entries(tag)
        )

    def record(
        self,
        tag: str,
        cause: TransitionCause,
        from_val: Any,
        to_val: Any,
        provenance: Provenance = Provenance.OBSERVED,
    ) -> None:
        """In-place advance of the entry table — seeding and direct callers.

        The loop's RECORD phase never lands here: it goes through :meth:`apply`,
        which returns the next compass as a value.  Semantics live in
        :func:`_table_record`.
        """
        self._entries, _ = _table_record(self._entries, tag, cause, from_val, to_val, provenance)

    def record_no_change(self, tag: str, cause: TransitionCause, from_val: Any) -> None:
        self._entries, _ = _table_no_change(self._entries, tag, cause, from_val)

    def commit_confirmed(self, entry: CompassEntry) -> None:
        """Insert a CONFIRMED entry built by ``outcome.confirmed_entry``.

        The only way a CONFIRMED provenance reaches the table.  Callers hand in a
        prebuilt :class:`CompassEntry`; anything else must go through
        :meth:`record` (which rejects CONFIRMED).
        """
        if entry.provenance is not Provenance.CONFIRMED:
            raise ValueError("commit_confirmed only accepts CONFIRMED entries")
        fv = _canon(entry.from_val)
        self._entries = self._entries.set((entry.tag, fv, entry.cause), entry.set(from_val=fv))

    def apply(self, observations: Iterable[CompassObservation]) -> tuple[Compass, bool]:
        """The RECORD write path: fold instrument observations into the compass.

        Instruments never call ``record``/``contradict`` themselves — they
        return :class:`CompassObservation` values and the loop applies them
        here, once per attempt / skiff round.  The fold advances the persistent
        entry table observation by observation (sequential, so within-batch
        evidence ordering is preserved — an edge recorded and then contradicted
        in one batch ends up demoted, as it did under the mutable table).

        **Contract — the no-new-knowledge signal.** Returns
        ``(next_compass, changed)``.  ``changed`` is True iff at least one entry
        was actually written (a fresh probe mark, a demoted edge, or a new/altered
        edge — re-recording an identical entry does *not* count).  The guarantee
        comes from the table ops, each of which reports whether it touched the
        table — **not** a blanket equality scan of the whole table.  When
        ``changed`` is False the returned compass **is** ``self`` (``x.apply(known)
        is x``), so a caller can trust identity as the "learned nothing" test.
        This lets the skiff decline a re-orient lap after a probe round that added
        nothing (a spin), without an identity side-channel.  ``self`` is never
        mutated; the caller's single ``ctx.compass, _ = compass.apply(...)``
        assignment is the commit point.
        """
        table = self._entries
        changed = False
        for obs in observations:
            if obs.kind == "edge":
                table, touched = _table_record(
                    table, obs.tag, obs.cause, obs.from_val, obs.to_val, Provenance.OBSERVED
                )
            elif obs.kind == "contradict":
                table, touched, _ = _table_contradict(table, obs.tag, obs.cause, obs.from_val)
            else:
                table, touched = _table_no_change(table, obs.tag, obs.cause, obs.from_val)
            changed |= touched
        if not changed:
            # No entry moved: the contract's identity guarantee, established from
            # the ops' own change flags rather than an equality scan of the table.
            return self, False
        return self._with_entries(table), True

    def _with_entries(self, entries: PMap) -> Compass:
        """A compass value carrying *entries*, sharing everything static."""
        if entries is self._entries:
            return self
        new = Compass.__new__(Compass)
        new._slices = self._slices
        new._action_tags = self._action_tags
        new._graphs = self._graphs
        new._entries = entries
        return new

    def contradict(self, tag: str, cause: TransitionCause, from_val: Any) -> bool:
        """Live evidence falsified a learned edge — demote it.

        A statically-seeded route (``seed_routes``) records the writer's edge
        without its unreadable enablers; when the live trial applies *cause*
        from *from_val* and the register does NOT reach the recorded
        destination, the entry is a disproven hypothesis and must not keep
        shadowing genuine (skiff-learned) edges in ``find_path``.  The edge is
        *demoted* to a CONTRADICTED tombstone rather than deleted — negative
        knowledge, not a blank — and stays a probe mark (the cause was genuinely
        tried).  Returns True if a live edge was demoted.
        """
        self._entries, _, removed = _table_contradict(self._entries, tag, cause, from_val)
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
            (fv, cause): entry.to_val
            for fv, cause, entry in self._tag_entries(tag)
            if entry.is_live
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
            for fv, cause, _entry in self._tag_entries(tag)
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
