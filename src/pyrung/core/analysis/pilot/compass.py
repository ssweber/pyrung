"""Thin immutable navigation facade and accumulated knowledge for PILOT."""

from __future__ import annotations

from collections import deque
from collections.abc import Iterable
from dataclasses import dataclass, field, replace
from enum import Enum
from typing import TYPE_CHECKING, Any, Literal, cast

from pyrsistent import PMap, PRecord, pmap
from pyrsistent import field as _precord_field

from pyrung.core.analysis.pilot.navigation_contracts import (
    EvidenceScope,
    NavigationConstraints,
    OrientationResult,
    OrientationWorld,
    TargetSpec,
    is_action,
    is_composite_action,
)
from pyrung.core.analysis.pilot.pipeline_graph import (
    ANY_FROM,
    ActionPair,
    PipelineSlice,
    StaticTransitionGraph,
    _applied_key,
    _canonical_applied,
)
from pyrung.core.analysis.sp_values import _values_match

if TYPE_CHECKING:
    from pyrung.core.analysis.pilot.conductivity import (
        ConductivityFront,
        ConductivityResearchRequest,
    )
    from pyrung.core.analysis.pilot.working_theory import (
        ConductivityResearchFinding,
        TheoryView,
    )

__all__ = [
    "WAIT",
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
TransitionCause = ActionPair | WaitCause


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
    # Exact executable world that produced this evidence. ``None`` is reserved
    # for deliberately global/seeded observations; runtime producers always
    # stamp their current frame key.
    world_key: tuple[Any, ...] | None = None
    # Exact pre-action values and the complete applied action set. Together
    # with ``world_key`` these distinguish executable contexts that the
    # projected state key intentionally omits, and let static-edge overlays
    # verify that the trial actually exercised the guarded artifact.
    context: tuple[ActionPair, ...] = ()
    applied: tuple[ActionPair, ...] = ()
    expectation: Any = None

    def __post_init__(self) -> None:
        if is_action(self.cause) and not self.applied:
            raise ValueError("action observations require a non-empty applied artifact")

    @property
    def applied_artifact(self) -> tuple[ActionPair, ...]:
        """Canonical form of the explicitly recorded executable artifact."""

        return _canonical_applied(self.applied)


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
    """Runtime evidence overlay for one immutable static edge identity.

    Narrower than transition entries: negative evidence attaches only when the
    trial exercised this edge's exact action/co-action set while its recorded
    concrete conditions held.
    """

    edge_id: tuple[Any, ...]
    status: Literal["confirmed", "contradicted", "no_change"]
    evidence_scope: EvidenceScope | None = None
    artifact_key: tuple[tuple[str, Any], ...] = ()


NavigationObservation = (
    CompassObservation
    | ActionNogoodObservation
    | ProbeExhaustedObservation
    | CoastObservation
    | StaticEdgeObservation
)


# ===========================================================================
# One entry per (world, snapshot, tag, from, cause, applied artifact) —
# provenance is the lifecycle
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
_ALL_CONTEXTS = object()


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
    """One learned transition (or probe mark) for one exact trial artifact.

    The persistent table key supplies the executable world and full
    pre-transition context; ``applied`` names the exact action overlay. A live
    entry has a destination; a NO_CHANGE or CONTRADICTED tombstone is skipped
    during traversal but remains evidence that this artifact was tried.
    Contradictory evidence demotes rather than deletes an entry.

    A CONFIRMED entry is minted only by ``outcome.confirmed_entry``; the
    general observation write path rejects that provenance, so
    confirmation cannot be asserted by a general observation writer.

    Scoping is local-first: a tombstone cannot erase another recipe or
    co-action context, but within its own exact context it overrides
    deliberately global seeded evidence, just as an exact-world destination
    supersedes a global destination.
    """

    tag = _precord_field(type=str)
    from_val = _precord_field()
    cause = _precord_field()
    applied = _precord_field(initial=())
    to_val = _precord_field()
    provenance = _precord_field(type=Provenance)
    expectation = _precord_field(initial=None)

    @property
    def is_live(self) -> bool:
        return self.provenance in _LIVE_PROVENANCE


def _action_sort_key(action: Any) -> tuple[tuple[str, str], ...]:
    """Total order key for one *action* — flat or skiff-learned composite.

    ``unprobed_actions`` sorts a set that can mix a flat action pair ``(tag,
    value)`` with a composite pair-probe cause ``((tag, value), (tag, value))``
    (:func:`is_composite_action`) — the skiff learns the latter as a joint
    cause.  Sorting the two shapes directly with the default tuple order
    compares element 0 of a flat action (a ``str``) against element 0 of a
    composite (a ``tuple``), which raises ``TypeError``.  Canonicalize both
    shapes to a tuple of ``(tag, value)`` pairs first — a flat action becomes
    a one-member tuple — then key on ``str``/``repr`` of each member so mixed
    value types (bool/int/str/float) can never reintroduce an unorderable
    comparison.

    Typed ``Any`` (like :func:`is_composite_action`) rather than ``ActionPair``:
    a composite cause is not structurally an ``ActionPair`` (``tuple[str, Any]``)
    — its first element is a tuple, not a ``str`` — even though it flows
    through call sites typed as ``ActionPair``/``TransitionCause``.
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
    evidence_scope: EvidenceScope | None,
    applied: tuple[ActionPair, ...],
    expectation: Any = None,
) -> tuple[PMap, bool]:
    """Write a live edge, overwriting only the same exact trial artifact.

    Learning an edge revives a matching CONTRADICTED tombstone and records the
    destination value.  The entry is its own probe mark, so there is no separate
    ``_probed`` write.

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
    key = (evidence_scope, tag, fv, cause, _applied_key(applied))
    entry = CompassEntry(
        tag=tag,
        from_val=fv,
        cause=cause,
        applied=applied,
        to_val=to_val,
        provenance=provenance,
        expectation=expectation,
    )
    if entries.get(key) == entry:
        return entries, False
    return entries.set(key, entry), True


def _table_no_change(
    entries: PMap,
    tag: str,
    cause: TransitionCause,
    from_val: Any,
    evidence_scope: EvidenceScope | None,
    applied: tuple[ActionPair, ...],
) -> tuple[PMap, bool]:
    """Probe mark only.

    Leaves any existing entry alone (a live edge must NOT be demoted by a
    no-change probe); creates a NO_CHANGE tombstone only where nothing was
    tried before.  Returns ``(next_table, changed)`` — ``changed`` is True only
    when a fresh probe mark was added.
    """
    fv = _canon(from_val)
    key = (evidence_scope, tag, fv, cause, _applied_key(applied))
    if key in entries:
        return entries, False
    return (
        entries.set(
            key,
            CompassEntry(
                tag=tag,
                from_val=fv,
                cause=cause,
                applied=applied,
                to_val=None,
                provenance=Provenance.NO_CHANGE,
            ),
        ),
        True,
    )


def _table_contradict(
    entries: PMap,
    tag: str,
    cause: TransitionCause,
    from_val: Any,
    evidence_scope: EvidenceScope | None,
    applied: tuple[ActionPair, ...],
) -> tuple[PMap, bool, bool]:
    """Demote every matching live edge to a CONTRADICTED tombstone.

    The evolver advances the persistent table and its ``.persistent()`` is the
    next value.  Returns ``(next_table, changed, demoted_any)`` — ``changed`` is
    True when a live edge was demoted *or* a fresh probe mark was added;
    ``demoted_any`` is True only for a live-edge demotion, which is what the
    public ``contradict`` reports.
    """
    evolver = entries.evolver()
    removed = False
    for key, entry in entries.items():
        if (
            key[0] == evidence_scope
            and key[1] == tag
            and key[3] == cause
            and _values_match(key[2], from_val)
            and key[4] == _applied_key(applied)
            and entry.is_live
        ):
            evolver[key] = entry.set(to_val=None, provenance=Provenance.CONTRADICTED)
            removed = True
    # Ensure the passed key carries a probe mark.  When it collapses onto a
    # just-demoted edge (bool/int keys share a PMap slot) it is already a
    # tombstone; otherwise record a bare NO_CHANGE probe.
    pkey = (evidence_scope, tag, _canon(from_val), cause, _applied_key(applied))
    probe_added = False
    if pkey not in entries:
        evolver[pkey] = CompassEntry(
            tag=tag,
            from_val=_canon(from_val),
            cause=cause,
            applied=applied,
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
    """Immutable static readings for one program.

    ``graphs`` remains the production-admitted opaque subset.
    ``chart_graphs`` is the generalized prover-confirmed read-only catalog;
    merely discovering one of those charts must neither admit it to current
    navigation nor make one of its inputs an independently Compass-owned
    action.
    """

    slices: tuple[PipelineSlice, ...] = ()
    graphs: tuple[StaticTransitionGraph, ...] = ()
    chart_graphs: tuple[StaticTransitionGraph, ...] = ()

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
        """Pair-level rejections proven in *world_key*.

        An explicit pair identity and a singleton Pulse both disprove one
        action pair. A joint Pulse is a distinct executable artifact: its
        primary pair remains eligible with a different co-action overlay.
        """

        pairs: set[ActionPair] = set()
        for identity in self.nogood_identities(world_key):
            if len(identity) == 2 and identity[0] == "pair":
                pairs.add(identity[1])
            elif len(identity) >= 2 and identity[0] == "pulse":
                applied = identity[1]
                if isinstance(applied, tuple) and len(applied) == 1:
                    pairs.add(applied[0])
        return frozenset(pairs)

    def act_is_nogood(self, world_key: tuple[Any, ...], identity: tuple[Any, ...]) -> bool:
        return identity in self.nogood_identities(world_key)

    def probe_count(self, world_key: tuple[Any, ...]) -> int:
        return int(self.probe_counts.get(world_key, 0))

    def coast_receipt(self, world_key: tuple[Any, ...]) -> str | None:
        return self.coast_receipts.get(world_key)

    def after_stable_context_change(self, world_key: tuple[Any, ...]) -> CompassKnowledge:
        """Expire empirical negatives scoped to a pre-setup input context.

        Runtime transition entries retain their full snapshot context. Act
        nogoods, probe counts, and terminal-coast receipts deliberately use the
        projected world key, which omits steerable values; an accepted stable
        setup therefore makes only those negative receipts stale.
        """

        return replace(
            self,
            act_nogoods=(
                self.act_nogoods.remove(world_key)
                if world_key in self.act_nogoods
                else self.act_nogoods
            ),
            probe_counts=(
                self.probe_counts.remove(world_key)
                if world_key in self.probe_counts
                else self.probe_counts
            ),
            coast_receipts=(
                self.coast_receipts.remove(world_key)
                if world_key in self.coast_receipts
                else self.coast_receipts
            ),
        )

    def tag_entries(
        self,
        tag: str,
        *,
        world_key: tuple[Any, ...] | None | object = _ALL_CONTEXTS,
        snapshot: dict[str, Any] | None = None,
        applied: tuple[ActionPair, ...] | None = None,
    ) -> Iterable[tuple[Any, TransitionCause, CompassEntry]]:
        """Entries for *tag*, optionally restricted to one applicable world.

        A deliberately global entry (scope ``None``) applies in every world.
        Scoped runtime evidence applies only in the exact world that produced
        it. Omitting ``world_key`` is an inspection operation and returns all
        receipts; navigation callers always pass a concrete key (or ``None``
        when querying only deliberately global seeded evidence).
        """
        if world_key is _ALL_CONTEXTS:
            for (
                _entry_scope,
                entry_tag,
                from_value,
                cause,
                _artifact,
            ), entry in self.entries.items():
                if entry_tag == tag:
                    yield from_value, cause, entry
            return

        # Resolve, do not union: an exact-world tombstone must suppress a
        # global live entry just as an exact-world destination supersedes a
        # global destination. Persistent-map iteration order is irrelevant.
        resolved: dict[tuple[Any, TransitionCause, Any], CompassEntry] = {}
        exact_scope = EvidenceScope.capture(
            world_key if isinstance(world_key, tuple) else None,
            snapshot.items() if snapshot is not None else None,
        )
        scopes = (None,) if world_key is None else (None, exact_scope)
        exact_artifact = _applied_key(applied) if applied is not None else None
        for scope in scopes:
            for (
                entry_scope,
                entry_tag,
                from_value,
                cause,
                artifact,
            ), entry in self.entries.items():
                if (
                    entry_scope == scope
                    and entry_tag == tag
                    and (scope is None or exact_artifact is None or artifact == exact_artifact)
                ):
                    resolved[(from_value, cause, artifact)] = entry
        for (from_value, cause, _artifact), entry in resolved.items():
            yield from_value, cause, entry

    def live_edges(
        self,
        tag: str,
        *,
        world_key: tuple[Any, ...] | None = None,
        snapshot: dict[str, Any] | None = None,
    ) -> dict[tuple[Any, TransitionCause], Any]:
        destinations: dict[tuple[Any, TransitionCause], list[Any]] = {}
        for from_value, cause, entry in self.tag_entries(
            tag, world_key=world_key, snapshot=snapshot
        ):
            if not entry.is_live:
                continue
            values = destinations.setdefault((from_value, cause), [])
            if not any(_values_match(entry.to_val, value) for value in values):
                values.append(entry.to_val)
        # Differing destinations from distinct exact artifacts are ambiguity,
        # not an invitation for persistent-map iteration order to select one.
        return {key: values[0] for key, values in destinations.items() if len(values) == 1}

    def has_transitions(
        self,
        tag: str,
        *,
        world_key: tuple[Any, ...] | None | object = _ALL_CONTEXTS,
        snapshot: dict[str, Any] | None = None,
    ) -> bool:
        return any(
            entry.provenance is not Provenance.NO_CHANGE
            for _from_value, _cause, entry in self.tag_entries(
                tag, world_key=world_key, snapshot=snapshot
            )
        )

    def find_path(
        self,
        tag: str,
        from_value: Any,
        to_value: Any,
        *,
        cause_allowed: Any = None,
        world_key: tuple[Any, ...] | None = None,
        snapshot: dict[str, Any] | None = None,
    ) -> list[TransitionCause] | None:
        live = self.live_edges(tag, world_key=world_key, snapshot=snapshot)
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

    def probed_actions(
        self,
        tag: str,
        from_value: Any,
        *,
        world_key: tuple[Any, ...] | None = None,
        snapshot: dict[str, Any] | None = None,
        applied: tuple[ActionPair, ...] | None = None,
    ) -> set[ActionPair]:
        return {
            cause
            for candidate_from, cause, _entry in self.tag_entries(
                tag,
                world_key=world_key,
                snapshot=snapshot,
                applied=applied,
            )
            if candidate_from == from_value and is_action(cause)
        }

    def unprobed_actions(
        self,
        tag: str,
        from_value: Any,
        available_actions: set[ActionPair] | frozenset[ActionPair],
        *,
        world_key: tuple[Any, ...] | None = None,
        snapshot: dict[str, Any] | None = None,
        applied_context: tuple[ActionPair, ...] | None = None,
    ) -> list[ActionPair]:
        ordered = sorted(available_actions, key=_action_sort_key)
        if applied_context is None:
            probed = self.probed_actions(tag, from_value, world_key=world_key, snapshot=snapshot)
            return [action for action in ordered if action not in probed]
        base = dict(applied_context)
        result: list[ActionPair] = []
        for action in ordered:
            applied = dict(base)
            members = (
                cast(tuple[ActionPair, ...], action) if is_composite_action(action) else (action,)
            )
            applied.update(members)
            tried = self.probed_actions(
                tag,
                from_value,
                world_key=world_key,
                snapshot=snapshot,
                applied=tuple(sorted(applied.items())),
            )
            if action not in tried:
                result.append(action)
        return result

    def transition_dest(
        self,
        tag: str,
        from_value: Any,
        cause: TransitionCause,
        *,
        world_key: tuple[Any, ...] | None = None,
        snapshot: dict[str, Any] | None = None,
    ) -> Any | None:
        for (candidate_from, candidate_cause), destination in self.live_edges(
            tag, world_key=world_key, snapshot=snapshot
        ).items():
            if candidate_cause == cause and _values_match(candidate_from, from_value):
                return destination
        return None

    def off_path_actions(
        self,
        tag: str,
        from_value: Any,
        to_value: Any,
        *,
        world_key: tuple[Any, ...] | None = None,
        snapshot: dict[str, Any] | None = None,
    ) -> set[ActionPair]:
        path = self.find_path(tag, from_value, to_value, world_key=world_key, snapshot=snapshot)
        if not path:
            return set()
        good_cause = path[0]
        table = self.live_edges(tag, world_key=world_key, snapshot=snapshot)
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

    def static_edge_status(
        self,
        edge: Any,
        *,
        evidence_scope: EvidenceScope | None,
    ) -> Literal["confirmed", "contradicted", "no_change"] | None:
        """Evidence for one static edge in one world.

        Exact-world evidence overrides a deliberately global seeded overlay.
        Callers capture the scope once for their fixed world/snapshot and never
        reconstruct the persistent-map storage key.
        """
        edge_id = edge.identity
        required_actions = () if edge.action is None else (edge.action, *edge.co_actions)
        scoped_key = (evidence_scope, _applied_key(required_actions), edge_id)
        if evidence_scope is not None and scoped_key in self.static_overlays:
            return self.static_overlays[scoped_key]
        return self.static_overlays.get(edge_id)

    def apply(
        self,
        observations: Iterable[NavigationObservation],
        *,
        _evidence_scopes: dict[int, EvidenceScope | None] | None = None,
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
                overlay_key = (
                    observation.edge_id
                    if observation.evidence_scope is None
                    else (
                        observation.evidence_scope,
                        observation.artifact_key,
                        observation.edge_id,
                    )
                )
                if static_overlays.get(overlay_key) != observation.status:
                    static_overlays = static_overlays.set(
                        overlay_key,
                        observation.status,
                    )
                    changed = True
            elif observation.kind == "edge":
                evidence_scope = (
                    _evidence_scopes[id(observation)]
                    if _evidence_scopes is not None
                    else EvidenceScope.capture(
                        observation.world_key,
                        observation.context,
                    )
                )
                applied = observation.applied_artifact
                table, touched = _table_record(
                    table,
                    observation.tag,
                    observation.cause,
                    observation.from_val,
                    observation.to_val,
                    Provenance.OBSERVED,
                    evidence_scope,
                    applied,
                    observation.expectation,
                )
                changed |= touched
            elif observation.kind == "contradict":
                evidence_scope = (
                    _evidence_scopes[id(observation)]
                    if _evidence_scopes is not None
                    else EvidenceScope.capture(
                        observation.world_key,
                        observation.context,
                    )
                )
                applied = observation.applied_artifact
                table, touched, _ = _table_contradict(
                    table,
                    observation.tag,
                    observation.cause,
                    observation.from_val,
                    evidence_scope,
                    applied,
                )
                changed |= touched
            else:
                evidence_scope = (
                    _evidence_scopes[id(observation)]
                    if _evidence_scopes is not None
                    else EvidenceScope.capture(
                        observation.world_key,
                        observation.context,
                    )
                )
                applied = observation.applied_artifact
                table, touched = _table_no_change(
                    table,
                    observation.tag,
                    observation.cause,
                    observation.from_val,
                    evidence_scope,
                    applied,
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
    def chart_graphs(self) -> tuple[StaticTransitionGraph, ...]:
        """Generalized static charts not yet admitted to navigation."""

        return self.catalog.chart_graphs

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

    def conductivity_front(self, theory_view: TheoryView | None) -> ConductivityFront | None:
        """Read ordered intrascan propagation without changing navigation knowledge."""

        from pyrung.core.analysis.pilot.conductivity import conductivity_front

        return conductivity_front(theory_view)

    def conductivity_research(
        self,
        theory_view: TheoryView | None,
    ) -> ConductivityResearchRequest | None:
        """Name an exact stopped-flow question unless its finding is retained."""

        from pyrung.core.analysis.pilot.conductivity import conductivity_research_request

        request = conductivity_research_request(self.conductivity_front(theory_view))
        if (
            request is not None
            and theory_view is not None
            and theory_view.has_research_finding(request.identity)
        ):
            return None
        return request

    def completed_conductivity_research(
        self,
        theory_view: TheoryView | None,
    ) -> ConductivityResearchFinding | None:
        """Return the finding for the exact current stopped-flow question."""

        if theory_view is None or not theory_view.research_findings:
            return None
        from pyrung.core.analysis.pilot.conductivity import conductivity_research_request

        request = conductivity_research_request(self.conductivity_front(theory_view))
        return theory_view.research_finding(request.identity) if request is not None else None

    def apply(self, observations: Iterable[NavigationObservation]) -> tuple[Compass, bool]:
        """Return a new facade when observations add durable knowledge."""

        supplied = tuple(observations)
        overlays: list[StaticEdgeObservation] = []
        evidence_scopes: dict[int, EvidenceScope | None] = {}
        # Producers deliberately share one immutable context object across an
        # observation batch.  Canonicalize that exact source scope once, then
        # reuse it for both static overlays and dynamic Compass knowledge.
        scope_cache: dict[
            int,
            tuple[tuple[ActionPair, ...], tuple[Any, ...] | None, EvidenceScope | None],
        ] = {}
        for observation in supplied:
            if not isinstance(observation, CompassObservation):
                continue
            # A WAIT no-change is only an intermediate dwell sample. Static
            # completion edges promise eventual motion, so one quiet scan
            # cannot falsify them. A changed WAIT destination, or an explicit
            # contradiction, is still meaningful overlay evidence.
            if observation.kind == "no_change" and not is_action(observation.cause):
                continue
            cached = scope_cache.get(id(observation.context))
            if (
                cached is not None
                and cached[0] is observation.context
                and cached[1] == observation.world_key
            ):
                evidence_scope = cached[2]
            else:
                evidence_scope = EvidenceScope.capture(
                    observation.world_key,
                    observation.context,
                )
                scope_cache[id(observation.context)] = (
                    observation.context,
                    observation.world_key,
                    evidence_scope,
                )
            evidence_scopes[id(observation)] = evidence_scope
            applied_artifact = observation.applied_artifact
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
                    if not edge.exercised_by(observation, applied_artifact):
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
                    overlays.append(
                        StaticEdgeObservation(
                            edge.identity,
                            status,
                            evidence_scope,
                            _applied_key(applied_artifact),
                        )
                    )
        knowledge, changed = self.knowledge.apply(
            (*supplied, *overlays),
            _evidence_scopes=evidence_scopes,
        )
        if not changed:
            return self, False
        return replace(self, knowledge=knowledge), True
