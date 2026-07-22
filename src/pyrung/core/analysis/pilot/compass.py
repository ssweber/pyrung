"""Thin immutable navigation facade and accumulated knowledge for PILOT."""

from __future__ import annotations

from collections import deque
from collections.abc import Iterable
from dataclasses import dataclass, field, replace
from enum import Enum
from typing import Any, Literal, TypeGuard

from pyrsistent import PMap, PRecord, pmap
from pyrsistent import field as _precord_field

from pyrung.core.analysis.pilot.charts import (
    ANY_FROM,
    Action,
    ActionPair,
    PipelineSlice,
    StaticTransitionGraph,
)
from pyrung.core.analysis.pilot.navigation import (
    NavigationConstraints,
    OrientationResult,
    OrientationWorld,
    TargetSpec,
)
from pyrung.core.analysis.sp_values import _values_match

__all__ = [
    "WAIT",
    "Action",
    "ActionPair",
    "ActionNogoodObservation",
    "Compass",
    "CompassKnowledge",
    "CompassEntry",
    "CompassObservation",
    "NavigationCatalog",
    "Provenance",
    "TransitionCause",
    "WaitCause",
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
    """One transition observation waiting to be applied by the drive loop.

    Execution and skiff probes produce observations without mutating the
    compass. The loop applies every observation, including those from rejected
    trials, so probe marks and contradictions survive a world revert.

    ``kind`` selects the write:

    - ``"edge"``       → a learned transition
    - ``"no_change"``  → a probe mark only
    - ``"contradict"`` → a falsified edge plus probe mark
    """

    kind: Literal["edge", "no_change", "contradict"]
    tag: str
    cause: TransitionCause
    from_val: Any
    to_val: Any = None


@dataclass(frozen=True)
class ActionNogoodObservation:
    """Empirical rejection of one act in one executable world."""

    world_key: tuple[Any, ...]
    identity: tuple[Any, ...]


@dataclass(frozen=True)
class ProbeExhaustedObservation:
    """One bounded probe round was consumed for this executable world."""

    world_key: tuple[Any, ...]


@dataclass(frozen=True)
class CoastObservation:
    """A terminal coast/dwell receipt that affects future orientation."""

    world_key: tuple[Any, ...]
    stop_reason: str


@dataclass(frozen=True)
class StaticEdgeObservation:
    """Runtime evidence overlay for one immutable static edge identity."""

    edge_id: tuple[Any, ...]
    status: Literal["confirmed", "contradicted", "no_change"]


NavigationObservation = (
    CompassObservation
    | ActionNogoodObservation
    | ProbeExhaustedObservation
    | CoastObservation
    | StaticEdgeObservation
)


# ===========================================================================
# One entry per (tag, from_val, cause) — provenance is the lifecycle
# ===========================================================================


class Provenance(Enum):
    """How a compass entry was established and whether it remains traversable.

    OBSERVED and CONFIRMED entries carry destinations. NO_CHANGE and
    CONTRADICTED entries are nontraversable tombstones, but still count as probe
    marks so a disproved or ineffective action is not sent again.
    """

    OBSERVED = "observed"  # a runtime motion applied by the drive loop
    CONFIRMED = "confirmed"  # minted only by outcome.confirmed_entry (verify)
    NO_CHANGE = "no_change"  # probe mark: the cause was tried and nothing moved
    CONTRADICTED = "contradicted"  # a falsified edge, kept as negative knowledge


# Live (traversable) provenances — the edges find_path/off_path/transition_dest
# walk.  A CONTRADICTED or NO_CHANGE entry is a tombstone: still a probe mark,
# never a destination.
_LIVE_PROVENANCE = frozenset({Provenance.OBSERVED, Provenance.CONFIRMED})


def _canon(value: Any) -> Any:
    """Canonicalize a transition-table key value from ``bool`` to ``int``.

    ``hash(True) == hash(1)`` and ``True == 1``, so a PMap already collapses the
    two forms into one slot; canonicalizing makes the stored key uniform so a
    later exact ``==`` read (``probed_actions``) never depends on which form was
    written.  Genuine value fuzz (graph BFS, ``ANY_FROM``) stays in
    ``_values_match`` — this only normalizes the bool/int duplicate.
    """
    return int(value) if isinstance(value, bool) else value


class CompassEntry(PRecord):
    """One learned transition (or probe mark) for a ``(tag, from_val, cause)``.

    The persistent record keeps learned knowledge independent from revertible
    PLC worlds. A live entry has a destination; a NO_CHANGE or CONTRADICTED
    tombstone is skipped during traversal but remains evidence that the cause
    was tried. Contradictory evidence demotes rather than deletes an entry.

    A CONFIRMED entry is minted only by ``outcome.confirmed_entry``; the
    general observation write path rejects that provenance, so
    confirmation cannot be asserted by a general observation writer.
    """

    tag = _precord_field(type=str)
    from_val = _precord_field()
    cause = _precord_field()
    to_val = _precord_field()
    provenance = _precord_field(type=Provenance)

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


def _action_sort_key(action: Any) -> tuple[tuple[str, str], ...]:
    """Total order key for one *action* — flat or skiff-learned composite.

    ``unprobed_actions`` sorts a set that can mix a flat ``Action`` ``(tag,
    value)`` with a composite pair-probe cause ``((tag, value), (tag, value))``
    (:func:`is_composite_action`) — the skiff learns the latter as a joint
    cause.  Sorting the two shapes directly with the default tuple order
    compares element 0 of a flat action (a ``str``) against element 0 of a
    composite (a ``tuple``), which raises ``TypeError``.  Canonicalize both
    shapes to a tuple of ``(tag, value)`` pairs first — a flat action becomes
    a one-member tuple — then key on ``str``/``repr`` of each member so mixed
    value types (bool/int/str/float) can never reintroduce an unorderable
    comparison.

    Typed ``Any`` (like :func:`is_composite_action`) rather than ``Action``:
    a composite cause is not structurally an ``Action`` (``tuple[str, Any]``)
    — its first element is a tuple, not a ``str`` — even though it flows
    through call sites typed as ``Action``/``TransitionCause``.
    """
    pairs: tuple[ActionPair, ...] = action if is_composite_action(action) else (action,)
    return tuple((str(t), repr(v)) for t, v in pairs)


# ===========================================================================
# Pure table operations — every write is persistent-table-in, persistent-
# table-out; the Compass methods and observation fold both delegate here so
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
    no-change probe); creates a NO_CHANGE tombstone only where nothing was
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
# Immutable static catalog and accumulated navigation knowledge
# ===========================================================================


@dataclass(frozen=True)
class NavigationCatalog:
    """Immutable static readings for one program."""

    slices: tuple[PipelineSlice, ...] = ()
    graphs: tuple[StaticTransitionGraph, ...] = ()

    @property
    def action_tags(self) -> frozenset[str]:
        if not self.slices:
            return frozenset()
        return frozenset().union(*(s.action_tags for s in self.slices))


@dataclass(frozen=True)
class CompassKnowledge:
    """Durable navigation-only evidence.

    Every field is persistent and survives PLC world reverts.  Static graphs
    and routes are intentionally absent: they belong to
    :class:`NavigationCatalog`.
    """

    entries: PMap = field(default_factory=pmap)
    act_nogoods: PMap = field(default_factory=pmap)
    probe_counts: PMap = field(default_factory=pmap)
    coast_receipts: PMap = field(default_factory=pmap)
    static_overlays: PMap = field(default_factory=pmap)

    def nogood_identities(self, world_key: tuple[Any, ...]) -> frozenset[tuple[Any, ...]]:
        return self.act_nogoods.get(world_key, frozenset())

    def nogood_pairs(self, world_key: tuple[Any, ...]) -> frozenset[ActionPair]:
        pairs: set[ActionPair] = set()
        for identity in self.nogood_identities(world_key):
            if len(identity) == 2 and identity[0] == "pair":
                pairs.add(identity[1])
            elif len(identity) >= 2 and identity[0] == "pulse":
                applied = identity[1]
                if applied:
                    pairs.add(applied[0])
        return frozenset(pairs)

    def act_is_nogood(self, world_key: tuple[Any, ...], identity: tuple[Any, ...]) -> bool:
        return identity in self.nogood_identities(world_key)

    def probe_count(self, world_key: tuple[Any, ...]) -> int:
        return int(self.probe_counts.get(world_key, 0))

    def coast_receipt(self, world_key: tuple[Any, ...]) -> str | None:
        return self.coast_receipts.get(world_key)

    def tag_entries(self, tag: str) -> Iterable[tuple[Any, TransitionCause, CompassEntry]]:
        for (entry_tag, from_value, cause), entry in self.entries.items():
            if entry_tag == tag:
                yield from_value, cause, entry

    def live_edges(self, tag: str) -> dict[tuple[Any, TransitionCause], Any]:
        return {
            (from_value, cause): entry.to_val
            for from_value, cause, entry in self.tag_entries(tag)
            if entry.is_live
        }

    def has_transitions(self, tag: str) -> bool:
        return any(
            entry.provenance is not Provenance.NO_CHANGE
            for _from_value, _cause, entry in self.tag_entries(tag)
        )

    def find_path(
        self,
        tag: str,
        from_value: Any,
        to_value: Any,
        *,
        cause_allowed: Any = None,
    ) -> list[TransitionCause] | None:
        live = self.live_edges(tag)
        if not live:
            return None
        if _values_match(from_value, to_value):
            return []
        queue: deque[tuple[Any, list[TransitionCause]]] = deque([(from_value, [])])
        visited: set[Any] = {from_value}
        while queue:
            state, path = queue.popleft()
            for (source, cause), destination in live.items():
                if (
                    not _values_match(source, state)
                    or destination in visited
                    or (cause_allowed is not None and not cause_allowed(source, cause, destination))
                ):
                    continue
                next_path = [*path, cause]
                if _values_match(destination, to_value):
                    return next_path
                visited.add(destination)
                queue.append((destination, next_path))
        return None

    def probed_actions(self, tag: str, from_value: Any) -> set[Action]:
        return {
            cause
            for candidate_from, cause, _entry in self.tag_entries(tag)
            if candidate_from == from_value and is_action(cause)
        }

    def unprobed_actions(
        self,
        tag: str,
        from_value: Any,
        available_actions: set[Action] | frozenset[Action],
    ) -> list[Action]:
        return sorted(
            available_actions - self.probed_actions(tag, from_value),
            key=_action_sort_key,
        )

    def transition_dest(
        self,
        tag: str,
        from_value: Any,
        cause: TransitionCause,
    ) -> Any | None:
        for (candidate_from, candidate_cause), destination in self.live_edges(tag).items():
            if candidate_cause == cause and _values_match(candidate_from, from_value):
                return destination
        return None

    def off_path_actions(self, tag: str, from_value: Any, to_value: Any) -> set[Action]:
        path = self.find_path(tag, from_value, to_value)
        if not path:
            return set()
        good_cause = path[0]
        table = self.live_edges(tag)
        on_path: set[Any] = {from_value}
        state = from_value
        for cause in path:
            destination = table.get((state, cause))
            if destination is not None:
                on_path.add(destination)
                state = destination
        return {
            cause
            for (candidate_from, cause), destination in table.items()
            if _values_match(candidate_from, from_value)
            and cause != good_cause
            and is_action(cause)
            and destination not in on_path
        }

    def apply(
        self,
        observations: Iterable[NavigationObservation],
    ) -> tuple[CompassKnowledge, bool]:
        """Fold runtime observations into a new durable knowledge value."""

        table = self.entries
        act_nogoods = self.act_nogoods
        probe_counts = self.probe_counts
        coast_receipts = self.coast_receipts
        static_overlays = self.static_overlays
        changed = False
        for observation in observations:
            if isinstance(observation, ActionNogoodObservation):
                current = act_nogoods.get(observation.world_key, frozenset())
                if observation.identity not in current:
                    act_nogoods = act_nogoods.set(
                        observation.world_key,
                        current | {observation.identity},
                    )
                    changed = True
            elif isinstance(observation, ProbeExhaustedObservation):
                count = int(probe_counts.get(observation.world_key, 0))
                probe_counts = probe_counts.set(observation.world_key, count + 1)
                changed = True
            elif isinstance(observation, CoastObservation):
                if coast_receipts.get(observation.world_key) != observation.stop_reason:
                    coast_receipts = coast_receipts.set(
                        observation.world_key,
                        observation.stop_reason,
                    )
                    changed = True
            elif isinstance(observation, StaticEdgeObservation):
                if static_overlays.get(observation.edge_id) != observation.status:
                    static_overlays = static_overlays.set(
                        observation.edge_id,
                        observation.status,
                    )
                    changed = True
            elif observation.kind == "edge":
                table, touched = _table_record(
                    table,
                    observation.tag,
                    observation.cause,
                    observation.from_val,
                    observation.to_val,
                    Provenance.OBSERVED,
                )
                changed |= touched
            elif observation.kind == "contradict":
                table, touched, _ = _table_contradict(
                    table,
                    observation.tag,
                    observation.cause,
                    observation.from_val,
                )
                changed |= touched
            else:
                table, touched = _table_no_change(
                    table,
                    observation.tag,
                    observation.cause,
                    observation.from_val,
                )
                changed |= touched
        if not changed:
            return self, False
        return (
            replace(
                self,
                entries=table,
                act_nogoods=act_nogoods,
                probe_counts=probe_counts,
                coast_receipts=coast_receipts,
                static_overlays=static_overlays,
            ),
            True,
        )


@dataclass(frozen=True)
class Compass:
    """Thin immutable facade over static readings and accumulated knowledge."""

    catalog: NavigationCatalog = field(default_factory=NavigationCatalog)
    knowledge: CompassKnowledge = field(default_factory=CompassKnowledge)

    @property
    def graphs(self) -> tuple[StaticTransitionGraph, ...]:
        return self.catalog.graphs

    @property
    def action_tags(self) -> frozenset[str]:
        return self.catalog.action_tags

    def orient(
        self,
        world: OrientationWorld,
        target: TargetSpec,
        constraints: NavigationConstraints,
    ) -> OrientationResult:
        """Read the current world and return exactly one navigation result."""
        from pyrung.core.analysis.pilot.orientation import orient

        return orient(self, world, target, constraints)

    def has_transitions(self, tag: str) -> bool:
        # True iff a real edge was ever recorded for *tag* (a CONTRADICTED
        # tombstone still counts — it *was* an edge), matching the old
        # ``tag in self._transitions`` (which stayed True after contradict
        # emptied the per-tag dict).  A tag carrying only NO_CHANGE probe marks
        # never had a transition, so it reads False as it did before.
        return self.knowledge.has_transitions(tag)

    def apply(self, observations: Iterable[NavigationObservation]) -> tuple[Compass, bool]:
        """Return a new facade when observations add durable knowledge."""

        supplied = tuple(observations)
        overlays: list[StaticEdgeObservation] = []
        for observation in supplied:
            if not isinstance(observation, CompassObservation):
                continue
            # A WAIT no-change is only an intermediate dwell sample. Static
            # completion edges promise eventual motion, so one quiet scan
            # cannot falsify them. A changed WAIT destination, or an explicit
            # contradiction, is still meaningful overlay evidence.
            if observation.kind == "no_change" and not is_action(observation.cause):
                continue
            for graph in self.catalog.graphs:
                if graph.role.channel_tag != observation.tag:
                    continue
                for edge in graph.edges:
                    # A wildcard source is a context-compressed static claim.
                    # One runtime world cannot safely confirm or contradict it
                    # globally; retain it until a grounded edge is available.
                    if edge.from_value is ANY_FROM:
                        continue
                    cause_matches = (
                        edge.action == observation.cause
                        if is_action(observation.cause)
                        else edge.action is None
                    )
                    if not (cause_matches and _values_match(edge.from_value, observation.from_val)):
                        continue
                    if observation.kind == "edge":
                        # An alternate observed destination does not globally
                        # falsify a statically guarded edge: this runtime world
                        # may simply lack that edge's enablers. Only matching
                        # motion confirms it; explicit contradiction evidence
                        # below owns falsification.
                        if not _values_match(edge.to_value, observation.to_val):
                            continue
                        status: Literal["confirmed", "contradicted", "no_change"] = "confirmed"
                    elif observation.kind == "contradict":
                        status = "contradicted"
                    else:
                        status = "no_change"
                    overlays.append(StaticEdgeObservation(edge.identity, status))
        knowledge, changed = self.knowledge.apply((*supplied, *overlays))
        if not changed:
            return self, False
        return replace(self, knowledge=knowledge), True

    def find_path(
        self,
        tag: str,
        from_val: Any,
        to_val: Any,
        *,
        cause_allowed: Any = None,
    ) -> list[TransitionCause] | None:
        """BFS shortest transition-cause sequence through the learned table.

        Traverses only **live** edges — CONTRADICTED and NO_CHANGE tombstones
        are skipped, so a falsified edge never shadows a genuine path (they used
        to be deleted; a tombstone that still matched would change behavior).
        """
        return self.knowledge.find_path(
            tag,
            from_val,
            to_val,
            cause_allowed=cause_allowed,
        )

    def unprobed_actions(
        self,
        tag: str,
        from_val: Any,
        available_actions: set[Action] | frozenset[Action],
    ) -> list[Action]:
        """Available actions not yet tried from *from_val* for *tag*.

        ``available_actions`` may mix flat actions with skiff-learned
        composite (pair-probe) causes — sort with :func:`_action_sort_key`,
        not the bare tuple order, so the two shapes never get compared
        directly (see its docstring for the crash that guards against).
        """
        return self.knowledge.unprobed_actions(tag, from_val, available_actions)

    def probed_actions(self, tag: str, from_val: Any) -> set[Action]:
        """Actions already probed from *from_val* for *tag*.

        Every entry key — live edge or tombstone — is a probe mark, so this
        reads the whole entry table (the old ``_probed`` set was exactly the
        union of every write's key).
        """
        return self.knowledge.probed_actions(tag, from_val)

    def transition_dest(
        self,
        tag: str,
        from_val: Any,
        cause: TransitionCause,
    ) -> Any | None:
        """Observed destination for one transition cause from *from_val*."""
        return self.knowledge.transition_dest(tag, from_val, cause)

    def off_path_actions(self, tag: str, from_val: Any, to_val: Any) -> set[Action]:
        """Actions known to move *tag* away from the BFS path toward *to_val*.

        Once we know the shortest path, any action from the current state
        that goes to a state NOT on that path (or with no path to the
        target) is off-path and should be tried after path actions.
        """
        return self.knowledge.off_path_actions(tag, from_val, to_val)
