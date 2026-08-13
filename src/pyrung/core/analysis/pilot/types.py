"""Cross-module protocols and state records for the PILOT package.

The records distinguish the revertible PLC world from search knowledge that
survives a revert, and carry iteration, trial, event, incident, and correction
data between reading, execution, verification, and recovery modules.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field, replace
from enum import Enum
from types import MappingProxyType
from typing import TYPE_CHECKING, Any, Literal, Protocol, runtime_checkable

from pyrsistent import PRecord, PVector, pvector
from pyrsistent import field as _precord_field

from pyrung.core.analysis.pilot.coast import CoastReceipt, CoastTriggerEvent
from pyrung.core.analysis.pilot.earned_work import EarnedWorkReceipt
from pyrung.core.analysis.pilot.working_theory import TheoryState, TheoryView

if TYPE_CHECKING:
    from pyrung.core.analysis.causal._rung_writes import ScanRungWriteProjection
    from pyrung.core.analysis.pdg import ProgramGraph
    from pyrung.core.analysis.pilot.bootstrap import (
        BootstrapDesignation,
        BootstrapDesignationSnapshot,
        BootstrapEffect,
        BootstrapEffectSnapshot,
    )
    from pyrung.core.analysis.pilot.compass import Compass, CompassObservation
    from pyrung.core.analysis.pilot.effects import (
        EffectExpectation,
        EffectObservation,
        EffectObservationSnapshot,
        EffectOccurrenceSnapshot,
    )
    from pyrung.core.analysis.pilot.evidence import PipelineRoles, TransitionEvidence
    from pyrung.core.analysis.pilot.navigation_contracts import (
        ActPolicy,
        Bearing,
        BearingObjective,
        TargetSpec,
    )
    from pyrung.core.analysis.pilot.outcome import TrialAssessment
    from pyrung.core.analysis.pilot.overlay import PilotRung
    from pyrung.core.analysis.pilot.progress import PendingDeparture
    from pyrung.core.analysis.pilot.requirements import (
        ActiveRequirement,
        ExpectationReceipt,
        FailedEffectReceipt,
    )
    from pyrung.core.analysis.pilot.trace import DomainPrior, TraceAction, TraceChoice
    from pyrung.core.analysis.pilot.world_key import _StateKeyConfig
    from pyrung.core.runner import PLC, Epoch, EpochQuery

# ---------------------------------------------------------------------------
# Type aliases
# ---------------------------------------------------------------------------

_ActionPair = tuple[str, Any]
_StateKey = tuple[Any, ...]


class MotionKind(Enum):
    """Execution semantics of a trial; event labels remain diagnostic only."""

    INTERVENTION = "intervention"
    COAST_TO_BEARING = "coast-to-bearing"
    COAST_HOLDING_WORLD = "coast-holding-world"

    @property
    def is_coast(self) -> bool:
        return self is not MotionKind.INTERVENTION


@dataclass(frozen=True)
class ChannelMotion:
    """One requested channel boundary and verification's owned landing."""

    channel_tag: str | None = None
    target_value: Any = None
    boundary: Any = None
    stop_reason: str | None = None

    @property
    def active(self) -> bool:
        return self.channel_tag is not None

    @property
    def reached(self) -> bool:
        return self.stop_reason == "reached"

    @property
    def departed(self) -> bool:
        return self.stop_reason == "departed"


@dataclass(frozen=True)
class RevisitCredential:
    """Consumable identity of one policy-admissible revisit transition.

    ``kind`` keeps different evidence protocols disjoint. ``transition`` holds
    only canonical semantic identities, and ``source_world`` includes the
    effective PILOT rung overlay.
    """

    kind: Literal["departure", "earned-work"]
    source_world: _StateKey
    act: tuple[Any, ...]
    transition: tuple[Any, ...]


@dataclass(frozen=True)
class ScanProgressReceipt:
    """Proof that one exact accepted scan advanced the selected working edge.

    ``productive_scan`` identifies S1, while ``landing_scan`` may be the one
    retained S2 look-ahead.  The receipt is source- and landing-scoped; it is
    not a general promise that later scans are productive.
    """

    source_scan: int
    productive_scan: int
    landing_scan: int
    kind: Literal[
        "target",
        "selected-producer",
        "frontier",
        "earned-work",
        "conductivity",
        "observation",
    ]
    source_world: _StateKey
    landing_world: _StateKey
    selected_act: tuple[Any, ...]
    distance_before: int
    distance_after: int | None = None
    landing_owns_tip: bool = True


# ---------------------------------------------------------------------------
# WalkContext — the read-side seam of a backward trace
# ---------------------------------------------------------------------------


@runtime_checkable
class WalkContext(Protocol):
    """What a read-side capability may consume from one backward trace.

    ``trace.py``'s ``_TraceEnv`` contains all constants threaded through one
    trace. A separate static reader consumes only the world-describing subset,
    not route or recursion controls such as writer locks, ``avoid_pred``,
    ``guard_memo``, ``max_depth``, ``harness``, or ``clear_only``.
    This protocol defines that subset:

    - ``snapshot`` — the live register frame guards are evaluated against;
    - ``pdg`` — the :class:`ProgramGraph` (``writers_of`` / ``rung_nodes`` / ``tags``);
    - ``program`` — the Program (``resolve_rung``, instruction bodies);
    - ``steerable`` — the free input tags a reader may treat as available levers;
    - ``opaque_loop`` — the pinned pipeline registers a reader folds into its own
      ``current_tags`` (``{target} | opaque_loop``);
    - ``prior`` — the prover-derived ``DomainPrior`` (``nd_domains`` / ``func_deps``)
      an *enumerating* reader needs for complete-domain soundness.

    ``_TraceEnv`` satisfies this protocol structurally, so callers pass it
    without an adapter. Keeping the protocol outside ``trace.py`` lets static
    read capabilities depend on a narrow interface without importing the trace
    recursion engine.
    """

    @property
    def snapshot(self) -> Mapping[str, Any]: ...

    @property
    def pdg(self) -> ProgramGraph: ...

    @property
    def program(self) -> Any: ...

    @property
    def steerable(self) -> frozenset[str]: ...

    @property
    def opaque_loop(self) -> frozenset[str]: ...

    @property
    def prior(self) -> DomainPrior | None: ...


@dataclass(frozen=True)
class WorldView:
    """Minimal :class:`WalkContext` assembled from one live frame."""

    snapshot: Mapping[str, Any]
    pdg: Any
    program: Any
    steerable: frozenset[str]
    opaque_loop: frozenset[str]
    prior: Any = None
    clear_only: frozenset[str] = frozenset()
    pipeline_internal_tags: frozenset[str] = frozenset()
    pipeline_roles: tuple[Any, ...] = ()
    avoid_pred: Any = None
    harness: Any = None


# ---------------------------------------------------------------------------
# avoid= — a union of independently-avoided conditions
# ---------------------------------------------------------------------------


def _avoid_member_true(pred: Callable[[dict[str, Any]], bool], state: dict[str, Any]) -> bool:
    try:
        return bool(pred(state))
    except Exception:
        return False


@dataclass(frozen=True)
class _AvoidMember:
    """One avoided snapshot condition and its optional coast fold metadata."""

    name: str
    pred: Callable[[dict[str, Any]], bool]
    tags: frozenset[str] = frozenset()
    # The normalized runtime Condition equivalent to ``pred``.  CoastSession
    # uses it only as fold/read metadata; ``pred`` remains authoritative.
    # Opaque callables have none and therefore retain the narrower
    # real-observed-scan contract.
    condition: Any | None = None


@dataclass(frozen=True)
class _AvoidPredicate:
    """The user's ``avoid=`` as a **union of exclusions**.

    A violation is an OR across members: a path that satisfies *any* member is
    excluded.  A composite prohibition (``avoid=And(A, B)``) is a single member,
    so only the combined state is excluded.  The object is callable (all three
    gates evaluate it against a snapshot), and ``violated`` returns the names of
    the members a snapshot trips so a decline can name the offending condition(s).
    """

    members: tuple[_AvoidMember, ...]

    def __call__(self, state: dict[str, Any]) -> bool:
        return any(_avoid_member_true(m.pred, state) for m in self.members)

    def violated(self, state: dict[str, Any]) -> tuple[str, ...]:
        return tuple(m.name for m in self.members if _avoid_member_true(m.pred, state))

    def violated_after_clear(
        self,
        start: dict[str, Any],
        state: dict[str, Any],
    ) -> tuple[str, ...]:
        """Members clear at trial start that are true in a later observation."""
        return tuple(
            member.name
            for member in self.members
            if not _avoid_member_true(member.pred, start) and _avoid_member_true(member.pred, state)
        )

    @property
    def names(self) -> tuple[str, ...]:
        return tuple(m.name for m in self.members)


class _World(PRecord):
    """The revertible half of the pilot's state — *the world*.

    ``knowledge commits, the world reverts``: every field here rolls back to a
    checkpoint on regression, and every field *not* here (compass, nogoods,
    journey, …) survives. Pilot rungs belong here: they change what the next
    scan means, so the same PLC tags under a different rung overlay are a
    different world.  A ``pyrsistent`` PRecord so the value is
    persistent: the ``committed_acts`` PVector is immutable, so once a checkpoint
    captures a world (``snapshot_world``) later appends build a fresh world value
    and never mutate the captured one — the pointer semantics revert relies on.

    ``work`` is a live runner fork (a mutable object); the world merely holds a
    *reference* to it.  The persistence lives in the structure around it — the
    step vectors and ``best_trend`` — which is exactly the bookkeeping the old
    scan-cutoff filtering hand-reconstructed on revert.
    """

    work = _precord_field()
    committed_acts = _precord_field()
    best_trend = _precord_field()
    pilot_rungs = _precord_field()
    # Committed scan-ids spent *waiting* — accepted bearing-coast / let-run spans.
    # Timer dwell is waiting, not searching (see ``coast._COAST_BUDGET``),
    # so invocation-relative search scans subtract this credit. An accepted
    # coast that rides a 39k-scan dwell must not bankrupt the search. Lives in
    # the world so a revert rewinds the credit together with the scans it
    # excused.
    dwell_scans = _precord_field()


@dataclass(frozen=True, eq=False)
class _CheckpointOwner:
    """Stable identity for one rollback receipt as its executable world changes."""


@dataclass(frozen=True)
class _Checkpoint:
    """A revert anchor: a *pointer* to a world value plus the facts the launch knew.

    ``world`` is the immutable :class:`_World` captured at creation
    (``_PilotState.snapshot_world``); revert is ``state.load_world(cp.world)`` —
    plain assignment, not a scan-cutoff reconstruction.

    ``objective`` is Orientation's complete target-relative receipt that
    justified banking this world. Source anchors carry the objective of the
    operation launched there; progress anchors carry the objective of the
    operation that reached them. Recovery keeps rollback ownership separate
    from the current incident's objective.
    """

    key: _StateKey
    world: _World
    trend: int
    objective: BearingObjective
    owner: _CheckpointOwner = field(
        default_factory=_CheckpointOwner,
        compare=False,
        repr=False,
    )


@dataclass(frozen=True)
class _RecoveryOrigin:
    """Exact rollback owner and bounded incident evidence for one recovery."""

    checkpoint_owner: _CheckpointOwner
    anchor_scan: int
    before_snap: Mapping[str, Any]


@dataclass(frozen=True)
class _CausalCheckpoint:
    """A target-owned source boundary retained without a progress judgment.

    Ordinary :class:`_Checkpoint` values are trend checkpoints: their
    ``trend`` and complete Bearing objective exist only after Orientation has
    read a world.  The cold-start boundary precedes that read, so retaining it
    as this narrower causal checkpoint avoids inventing target progress while
    preserving the exact executable world needed by later occurrence-scoped
    recovery.
    """

    # ``None`` is explicit when the prover supplied no pre-orientation key
    # projection.  The frozen world remains exact; an empty tuple would falsely
    # claim a valid (and globally colliding) executable-world identity.
    key: _StateKey | None
    world: _World
    objective: BearingObjective
    # Exact external configuration visible at the source boundary. Runner
    # forks intentionally do not retain mutable patch/force managers, so
    # requirement authority must travel as immutable checkpoint provenance.
    configured_inputs: frozenset[str] = frozenset()
    owner: _CheckpointOwner = field(
        default_factory=_CheckpointOwner,
        compare=False,
        repr=False,
    )


def _immutable_bootstrap_fact(value: Any) -> Any:
    """Detach one diagnostic fact from mutable execution-owned objects."""

    if value is None or isinstance(value, bool | int | float | str | bytes):
        return value
    if isinstance(value, tuple | list):
        return tuple(_immutable_bootstrap_fact(item) for item in value)
    if isinstance(value, set | frozenset):
        return tuple(sorted((_immutable_bootstrap_fact(item) for item in value), key=repr))
    if isinstance(value, Mapping):
        return MappingProxyType(
            {
                _immutable_bootstrap_fact(key): _immutable_bootstrap_fact(member)
                for key, member in value.items()
            }
        )
    # Tag values are scalar in the execution model. Keep diagnostics inert if
    # an extension supplies a richer object instead of exposing that object.
    return repr(value)


@dataclass(frozen=True)
class BootstrapAccessSnapshot:
    """One deeply detached read or write in bootstrap execution order."""

    scan: int
    ordinal: int
    run_order: int
    rung: tuple[str | None, int]
    kind: Literal["read", "write"]
    tag: str
    values: tuple[Any, ...]


@dataclass(frozen=True)
class BootstrapExecutionSnapshot:
    """Safe event view of one internal bootstrap execution receipt.

    No runner, projection, run, instruction, or occurrence object crosses the
    event boundary. Mapping values and nested composite facts are recursively
    copied and frozen.
    """

    source_scan: int
    landing_scan: int
    source_world_key: Any
    objective: tuple[str, Any]
    objective_frontier: tuple[_ActionPair, ...]
    source: Mapping[str, Any]
    landing: Mapping[str, Any]
    ordered_accesses: tuple[BootstrapAccessSnapshot, ...]
    designations: tuple[BootstrapDesignationSnapshot, ...]
    appeared_effects: tuple[BootstrapEffectSnapshot, ...]


@dataclass(frozen=True)
class _BootstrapExecution:
    """Exact receipt for PILOT's single cold-start program scan.

    This is execution evidence plus factual bootstrap observation; it records
    no operator action, expectation, progress judgment, or correction.
    ``projection`` is the executor-owned ordered access journal for
    ``scan_before -> scan_after``; ``landing`` is copied from that projection's
    exit boundary.
    ``execution_epoch`` and its detached ``execution_owner`` retain the runner's
    existing causal identity and replay surface even after the live world
    restores the source checkpoint.
    """

    checkpoint: _CausalCheckpoint
    scan_before: int
    scan_after: int
    projection: ScanRungWriteProjection = field(compare=False, repr=False)
    landing: Mapping[str, Any]
    designations: tuple[BootstrapDesignation, ...]
    appeared_effects: tuple[BootstrapEffect, ...]
    execution_epoch: Epoch = field(compare=False, repr=False)
    execution_owner: EpochQuery = field(compare=False, repr=False)
    route_bound: bool = False

    def __post_init__(self) -> None:
        if self.scan_after != self.scan_before + 1:
            raise ValueError("bootstrap execution must contain exactly one scan")
        if self.projection.scan_id != self.scan_after:
            raise ValueError("bootstrap projection does not match its landing scan")
        if self.execution_owner.epoch is not self.execution_epoch:
            raise ValueError("bootstrap execution owner does not match its epoch")
        if not self.execution_epoch.first_scan <= self.scan_after <= self.execution_epoch.last_scan:
            raise ValueError("bootstrap execution epoch does not own its landing scan")
        object.__setattr__(self, "landing", MappingProxyType(dict(self.landing)))

    def diagnostic_snapshot(self) -> BootstrapExecutionSnapshot:
        """Return a detached immutable event view of this causal evidence."""

        from pyrung.core.analysis.pilot.bootstrap import designation_snapshot

        accesses: list[BootstrapAccessSnapshot] = [
            BootstrapAccessSnapshot(
                scan=read.scan_id,
                ordinal=read.ordinal,
                run_order=read.run_order,
                rung=(read.rung_id.subroutine, read.rung_id.rung_index),
                kind="read",
                tag=read.occurrence.name,
                values=(_immutable_bootstrap_fact(read.occurrence.value),),
            )
            for read in self.projection.reads
        ]
        accesses.extend(
            BootstrapAccessSnapshot(
                scan=write.scan_id,
                ordinal=write.ordinal,
                run_order=write.run_order,
                rung=(write.rung_id.subroutine, write.rung_id.rung_index),
                kind="write",
                tag=write.transition.tag_name,
                values=(
                    _immutable_bootstrap_fact(write.transition.from_value),
                    _immutable_bootstrap_fact(write.transition.to_value),
                ),
            )
            for write in self.projection.writes
        )
        accesses.sort(key=lambda access: access.ordinal)
        objective = self.checkpoint.objective
        return BootstrapExecutionSnapshot(
            source_scan=self.scan_before,
            landing_scan=self.scan_after,
            source_world_key=_immutable_bootstrap_fact(self.checkpoint.key),
            objective=(
                objective.target.tag,
                _immutable_bootstrap_fact(objective.target.value),
            ),
            objective_frontier=tuple(
                (tag, _immutable_bootstrap_fact(value)) for tag, value in objective.frontier
            ),
            source=MappingProxyType(
                {
                    tag: _immutable_bootstrap_fact(value)
                    for tag, value in self.projection.entry_tags.items()
                }
            ),
            landing=MappingProxyType(
                {tag: _immutable_bootstrap_fact(value) for tag, value in self.landing.items()}
            ),
            ordered_accesses=tuple(accesses),
            designations=tuple(designation_snapshot(item) for item in self.designations),
            appeared_effects=tuple(
                effect.diagnostic_snapshot() for effect in self.appeared_effects
            ),
        )


# ---------------------------------------------------------------------------
# Recorded step
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _Step:
    # The inputs physically applied for this step — the candidate plus its
    # co-actions (command button + one-shot edge gate), i.e. ``ActPolicy.applied``,
    # not only the policy's primary candidate. Named ``inputs`` (matching the prover's
    # reachability step) so the recording site can't confuse the two.
    inputs: dict[str, Any]
    scan_before: int
    scan_after: int

    @property
    def scans(self) -> int:
        return self.scan_after - self.scan_before


# ---------------------------------------------------------------------------
# Investigation incident — shared by investigate.py (builds it) and
# corrections.py (consumes it); lives here so neither module imports the other.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class BearingDeparture:
    """One fact that held at the incident anchor and later departed."""

    tag: str
    value: Any
    scan: int | None


@dataclass(frozen=True)
class DeviationIncident:
    """The bounded window where verify observed a loss of bearing."""

    anchor_scan: int
    departure_scan: int | None
    end_scan: int
    action: tuple[_ActionPair, ...]
    bearing: tuple[_ActionPair, ...]
    before_snap: Mapping[str, Any]
    after_snap: Mapping[str, Any]
    # Complete factual movement set: every timeline transition plus every
    # before/after endpoint difference. Consumers filter it locally.
    changed_tags: tuple[str, ...]
    departures: tuple[BearingDeparture, ...]
    # The macro-state register whose departure IS the incident (the bearing /
    # terminal-letrun channel tag) — other departures downstream of it are
    # collateral.  Hypothesis ranking keys causal primacy off its cause chain.
    channel_tag: str | None = None
    # The recorded session events inside the window (CoastTriggerEvents, ordered,
    # same-scan groups preserved).  This is the incident's evidence: a
    # fire-then-reset pulse is two transitions here, never a net no-op.
    timeline: tuple[Any, ...] = ()
    # Exact conditions on the retained writer occurrence.  Retained-prefix
    # recovery projects only the corrected direct conjuncts out of this tuple;
    # the remaining terms are the correction's executable lifetime.
    occurrence_conditions: tuple[Any, ...] = ()
    occurrence_writer: tuple[str | None, int] | None = None


# ---------------------------------------------------------------------------
# Events
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class PilotEvent:
    """Structured diagnostic event emitted by :func:`pilot_events`.

    The payload intentionally carries Python objects where useful instead of a
    pre-rendered text log.  Callers can decide how much to display.
    """

    kind: str
    scan: int
    data: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class TagChange:
    """A single tag value transition between two snapshots."""

    tag: str
    before: Any
    after: Any


@dataclass(frozen=True)
class PilotGateEvent:
    """Structured result from one candidate acceptance gate."""

    event: str
    detail: str = ""
    # The exact values the gate read to reach this verdict.  ``detail`` remains
    # the compact human caption; evidence is the durable machine-readable
    # ground used by decision skeletons and post-run diagnosis.
    evidence: Mapping[str, Any] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Context / state / frame
# ---------------------------------------------------------------------------


@dataclass
class _PilotContext:
    target: TargetSpec
    pdg: ProgramGraph
    program: Any
    steerable: frozenset[str]
    edge_tags: set[str]
    resting: dict[str, Any]
    nd_domains: dict[str, tuple[Any, ...]] | None
    domain_prior: DomainPrior | None
    evidence: TransitionEvidence | None
    # A compass *value*, replaced once per attempt / skiff round at the loop's
    # observation-application point (``ctx.compass, _ = ctx.compass.apply(...)``) — never a shared
    # mutable advanced behind readers' backs.  Knowledge commits, the world
    # reverts.
    compass: Compass
    opaque_loop: frozenset[str]
    pipeline_roles: tuple[PipelineRoles, ...]
    pipeline_internal_tags: frozenset[str]
    # The root route selected for this current-world bearing. Orientation
    # replaces it when comparing inferred alternatives; verification and replay
    # keep that exact writer/OR path for the duration of the bearing.
    route: TraceChoice | None
    # Generic caller-owned action exclusions for the current orientation read.
    # Public ``how()`` supplies none; other navigation clients may constrain an
    # exact action without implying a retained route choice.
    blocked_actions: frozenset[_ActionPair]
    # Relative count of new PILOT search scans allowed for this invocation.
    # Accepted productive dwell does not consume it.
    max_scans: int
    key_config: _StateKeyConfig | None = None
    avoid_pred: Any = None
    # A disposable retained-composition continuation observes only whether it
    # exposes another retained occurrence. The outer committed attempt owns
    # action-edge learning and its deep causal attribution.
    collect_action_attribution: bool = True
    # Clear-only (ack-cleared momentary) command tags — the pulse-treatment set.
    # Kept off prerequisite holds (options.py) and off preferred init/reset
    # writer selection (trace._rank_writers): a momentary command, never a hold.
    clear_only: frozenset[str] = frozenset()
    # Phase-4 scheduling knowledge exposed read-only to Orientation.  It is
    # not an executable overlay and candidate construction must not mutate it.
    active_requirements: tuple[ActiveRequirement, ...] = ()
    # Caller-owned patches/forces present when this drive was prepared. This
    # survives the initial execution fork only as provenance; every causal
    # checkpoint freezes its own immutable copy.
    configured_inputs: frozenset[str] = frozenset()
    # Read-only static charts discovered from every prover-confirmed stepping
    # channel. Unlike ``pipeline_roles``, these do not define Trace opacity or
    # pipeline-internal tags.
    chart_roles: tuple[PipelineRoles, ...] = ()
    # Detached lifecycle projection supplied afresh for each orientation read.
    # It contains no executable world or retained navigation future.
    theory_view: TheoryView | None = None
    # Drive-resolved live requirements for one exceptional temporal read.
    # Ordinary reads leave this empty and pay no temporal-branch cost.
    temporal_requirements: tuple[ActiveRequirement, ...] = ()
    temporal_source_anchor: tuple[Any, Any] | None = None


@dataclass(frozen=True)
class _ExecutionEvidence:
    """PLC-free evidence from the final execution VERIFY accepted.

    Navigation's policy remains a declaration. This record contains only the
    final observed operation window, copied away from the mutable execution
    fork after excursion replay has selected the pulse that will be committed.
    """

    before_snap: Mapping[str, Any]
    after_snap: Mapping[str, Any]
    channel_motion: ChannelMotion
    coast_receipt: CoastReceipt | None
    timeline: tuple[CoastTriggerEvent, ...]
    effect_observations: tuple[EffectObservationSnapshot, ...] = ()
    # The coast operation navigation actually executed.  ``channel_motion``
    # may be rebound to the semantic channel that owned an ejection; replay
    # must still seek this original boundary while watching that incident
    # channel for another departure.
    replay_motion: ChannelMotion = field(default_factory=ChannelMotion)
    scan_progress: ScanProgressReceipt | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "before_snap", MappingProxyType(dict(self.before_snap)))
        object.__setattr__(self, "after_snap", MappingProxyType(dict(self.after_snap)))
        object.__setattr__(self, "timeline", tuple(self.timeline))
        object.__setattr__(self, "effect_observations", tuple(self.effect_observations))

    @property
    def accelerators(self) -> tuple[_ActionPair, ...]:
        """Exact fold writes derived from the typed coast receipt."""
        return self.coast_receipt.advances if self.coast_receipt is not None else ()


@dataclass(frozen=True)
class _StepContext:
    """Semantic and evidentiary context owned by one committed operation."""

    policy: ActPolicy
    execution: _ExecutionEvidence
    frontier_tags: tuple[str, ...] = ()
    # Exact pilot rungs present during this step. Kept as ``Any`` here to
    # avoid coupling the state container back to the PilotRung implementation.
    pilot_rungs: tuple[Any, ...] = ()

    @property
    def steady_holds(self) -> tuple[str, ...]:
        """Concise view derived from the exact executable rung evidence."""
        return tuple(dict.fromkeys(rung.dest for rung in self.pilot_rungs))


@dataclass(frozen=True)
class _CommittedAct:
    """One accepted operation and every physical replay step it produced.

    A rising-edge intervention may require a release step followed by the
    requested pulse. Both steps share one operation context; keeping them here
    prevents consumers from reconstructing that ownership by matching scans.
    """

    steps: tuple[_Step, ...]
    context: _StepContext

    def __post_init__(self) -> None:
        if not self.steps:
            raise ValueError("a committed act must own at least one replay step")


@dataclass(frozen=True)
class _HoldLogEntry:
    """One hold installation event — append-only, survives reverts."""

    scan: int
    source: str
    pilot_rungs: tuple[PilotRung, ...]

    @property
    def tags(self) -> tuple[_ActionPair, ...]:
        """Concise recording view derived from the installed executable form."""
        return tuple((rung.dest, rung.value) for rung in self.pilot_rungs)


class CorrectionStatus(Enum):
    """Evidence maturity for an investigation-owned overlay."""

    PROBATIONARY = "probationary"
    ACTIVE = "active"
    REVOKED = "revoked"

    @property
    def effective(self) -> bool:
        """Whether the correction still participates in the live overlay."""
        return self is not CorrectionStatus.REVOKED


@dataclass(frozen=True)
class _CorrectionReceipt:
    """Bounded replay proof and lifecycle for one investigation correction."""

    receipt_id: int
    origin_key: _StateKey
    correction: _ConfirmedCorrection
    status: CorrectionStatus = CorrectionStatus.PROBATIONARY
    admitted_origins: frozenset[_StateKey] = frozenset()

    @property
    def identity(self) -> tuple[tuple[Any, ...], ...]:
        return self.correction.identity

    @property
    def pilot_rungs(self) -> tuple[PilotRung, ...]:
        return self.correction.pilot_rungs

    @property
    def sources(self) -> tuple[str, ...]:
        return self.correction.sources

    @property
    def justification(self) -> str:
        return self.correction.justification


@dataclass(frozen=True)
class _ConfirmedCorrection:
    """One replay-proven correction, including its exact executable lifetime."""

    identity: tuple[tuple[Any, ...], ...]
    pilot_rungs: tuple[PilotRung, ...]
    sources: tuple[str, ...]
    justification: str


@dataclass(frozen=True)
class _ContinuationCheckpoint:
    """One already-executed fresh-read boundary in a recovery continuation."""

    scan_id: int
    world_key: _StateKey
    kind: Literal["local_repair", "unchanged_coast", "program_input_handoff", "target_prefix"]
    execution_epoch: Any
    execution_owner: Any
    landing_occurrence: EffectOccurrenceSnapshot | None = None

    @property
    def program_step_certified(self) -> bool:
        return self.kind != "local_repair"


@dataclass(frozen=True)
class _RecoveryContinuation:
    """PLC-free authority carried from one locally repaired causal source.

    Checkpoints describe only worlds which ordinary Orientation has already
    reread and committed.  No future action, ordinal, projection, or predicted
    snapshot belongs to this receipt.
    """

    checkpoint_owner: Any
    source_world_key: _StateKey
    checkpoints: tuple[_ContinuationCheckpoint, ...]

    @property
    def tip(self) -> _ContinuationCheckpoint:
        return self.checkpoints[-1]


@dataclass
class _PilotState:
    # ── The world (reverts) ──
    # ``work`` / ``committed_acts`` / ``best_trend`` live inside a single
    # persistent :class:`_World` value. A checkpoint captures the whole world at
    # once and revert restores it by assignment (``snapshot_world`` /
    # ``load_world``). Flat steps are a read-only public/replay view derived from
    # the operation records below.
    world: _World
    # ── Knowledge (commits — never rolled back on revert) ──
    key_config: _StateKeyConfig | None
    seen_keys: set[_StateKey]
    checkpoints: list[_Checkpoint]
    watch_tags: list[str]
    # Exact executable boundary where this drive invocation began. Unlike the
    # optional bootstrap receipt, this exists at every scan number so a live
    # DAP/runner invocation can rebase an accepted early transaction.
    invocation_checkpoint: _CausalCheckpoint | None = None
    # Execution-only receipt for the optional cold-start ``0 -> 1`` scan.
    # Knowledge side: later world reverts must not erase the retained causal
    # source or reinterpret the immutable execution projection.
    bootstrap_execution: _BootstrapExecution | None = None
    # Failed-effect scheduling knowledge is append-only invocation knowledge.
    # It deliberately sits outside ``_World`` so checkpoint restore cannot
    # erase or reinterpret an exact execution receipt.
    active_requirements: list[ActiveRequirement] = field(default_factory=list)
    expectation_receipts: list[ExpectationReceipt] = field(default_factory=list)
    failed_effect_receipts: list[FailedEffectReceipt] = field(default_factory=list)
    # Exact source scans retained only when a temporal interpretation makes
    # them a future working edge. They are evidence/checkpoints, never cached
    # Bearings or executable suffixes.
    temporal_checkpoints: list[_CausalCheckpoint] = field(default_factory=list)
    # Immutable WorkingTheory knowledge. It is deliberately not part of
    # ``_World``: checkpoint restore must not erase observed lifecycle facts.
    # Some exact temporal facts now guide Compass, while optional lifecycle
    # recording remains isolated at its explicit adapter boundary.
    theory_state: TheoryState = field(default_factory=TheoryState)
    # Exact Phase-5 local schedules already admitted from one causal source.
    # Attempt identity is knowledge-side so restoring that source cannot turn
    # the same failed repair into another lap.
    requirement_repair_attempts: set[tuple[Any, ...]] = field(default_factory=set)
    # A locally repaired source may continue through fresh, exact current-world
    # reads.  This receipt is knowledge, not retained executable work.
    recovery_continuation: _RecoveryContinuation | None = None
    # Exact current-world acts rejected by obligation/requirement proof. These
    # are admissibility receipts, not empirical impossibility/nogood claims.
    # Their EvidenceScope includes the complete pre-action input context, so a
    # corrective level change admits the act anew while an unchanged replay
    # remains suppressed.
    proof_rejected_acts: set[tuple[Any, tuple[Any, ...]]] = field(default_factory=set)
    # Invocation-local knowledge: reverting a handled transition must not
    # authorize the same source/action/evidence occurrence for another lap.
    consumed_revisits: set[RevisitCredential] = field(default_factory=set)
    # Physical scan where this PILOT invocation began. Search budgets are
    # relative to this anchor; accepted productive dwell is removed separately
    # by ``dwell_scans`` as the world advances and reverts.
    search_start_scan: int = 0
    last_wait_log: tuple[Any, ...] | None = None
    # Reporting-only provenance from the most recently selected current-world
    # bearing. It never feeds Orientation or constrains a later read.
    recorded_root_route: TraceChoice | None = None
    # The target-relative earned-work model (earned_work.py) — event-earned
    # ordinals the threshold-masked search key aliases.  Static knowledge,
    # built once at loop init; a None/empty model degrades consumers (verify
    # spin/cycle gates, departure classification) to key-only behavior.
    earned_work: Any = None
    # A clean program departure awaiting stronger progress evidence. It is
    # promoted on advance, regressed only on evidence of loss, and otherwise
    # rolled back on expiry without manufacturing a nogood.
    pending_departure: PendingDeparture | None = None
    # Append-only log of every committed step, including attempts later reverted.
    # ``steps`` (the world) is the clean, sequentially-replayable path (restored
    # to the checkpoint's on revert); ``journey`` keeps the full "tried this,
    # ejected, learned, retried" record surfaced on the resulting plan.
    journey: list[_Step] = field(default_factory=list)
    hold_log: list[_HoldLogEntry] = field(default_factory=list)
    # Investigation corrections are hypotheses with lifecycle, not irrevocable
    # facts. The receipt journal survives world reverts; active rungs remain in
    # the world and are removed when a later incident causally revokes a receipt.
    correction_receipts: list[_CorrectionReceipt] = field(default_factory=list)
    correction_nogoods: dict[
        _StateKey,
        set[tuple[tuple[Any, ...], ...]],
    ] = field(default_factory=dict)
    # Names of ``avoid=`` conditions that excluded a candidate/hold/scan somewhere
    # in the drive (Knowledge side — commits, never reverted).  A terminal stuck
    # or budget-exhausted decline reads this so the miss names the violated avoid
    # condition(s) rather than a bare ``stuck``.
    avoid_names: set[str] = field(default_factory=set)
    # Relational lever reports per steered tag (``TraceAction.note``,
    # last-write-wins) — the "held Band < -100.0 to satisfy PV < Lower (e.g., …)"
    # lines the plan journal attaches to matching steps.  Knowledge side: it is
    # not part of the world, so revert leaves it in place, like the compass.
    lever_notes: dict[str, str] = field(default_factory=dict)

    # ── World access: bare-name reads/writes route through ``self.world`` ──
    @property
    def work(self) -> PLC:
        return self.world.work

    @work.setter
    def work(self, value: PLC) -> None:
        self.world = self.world.set(work=value)

    @property
    def committed_acts(self) -> PVector[_CommittedAct]:
        return self.world.committed_acts

    @committed_acts.setter
    def committed_acts(self, value: Any) -> None:
        self.world = self.world.set(committed_acts=pvector(value))

    @property
    def steps(self) -> PVector[_Step]:
        """Flattened public/replay view derived from committed operation owners."""
        return pvector(step for act in self.committed_acts for step in act.steps)

    def extend_last_step(self, scan_after: int) -> None:
        """Extend the last operation's final physical step to a settled landing."""
        if not self.committed_acts:
            return
        act = self.committed_acts[-1]
        last = act.steps[-1]
        final_step = replace(last, scan_after=scan_after)
        final_act = replace(act, steps=(*act.steps[:-1], final_step))
        if self.journey and self.journey[-1] is last:
            self.journey[-1] = final_step
        self.committed_acts = self.committed_acts.set(len(self.committed_acts) - 1, final_act)

    @property
    def best_trend(self) -> int | None:
        return self.world.best_trend

    @best_trend.setter
    def best_trend(self, value: int | None) -> None:
        self.world = self.world.set(best_trend=value)

    @property
    def pilot_rungs(self) -> PVector[Any]:
        return self.world.pilot_rungs

    @pilot_rungs.setter
    def pilot_rungs(self, value: Any) -> None:
        """Enter a new executable overlay world at the current scan boundary.

        Retained scans belong to the runner that actually executed them.  A
        changed PILOT overlay therefore gets a child runner whose causal parent
        is the old world; mutating the old runner would make on-demand causal
        reconstruction replay its history under the new overlay.
        """
        from pyrung.core.analysis.pilot.overlay import fork_with_pilot_rungs

        materialized = pvector(value)
        if materialized == self.world.pilot_rungs:
            return
        self.world = self.world.set(
            work=fork_with_pilot_rungs(self.world.work, materialized),
            pilot_rungs=materialized,
        )

    @property
    def dwell_scans(self) -> int:
        return self.world.dwell_scans

    @dwell_scans.setter
    def dwell_scans(self, value: int) -> None:
        self.world = self.world.set(dwell_scans=value)

    @property
    def search_scans(self) -> int:
        """New scans spent searching during this PILOT invocation."""

        return self.search_scans_at(self.work.state.scan_id)

    def search_scans_at(self, scan_id: int) -> int:
        """Search scans spent by the time a live or tentative fork reaches *scan_id*."""

        return scan_id - self.search_start_scan - self.dwell_scans

    def remaining_search_scans(self, max_scans: int, *, scan_id: int | None = None) -> int:
        """Search budget remaining at the live world or a tentative fork scan."""

        at_scan = self.work.state.scan_id if scan_id is None else scan_id
        return max(0, max_scans - self.search_scans_at(at_scan))

    def snapshot_world(self) -> _World:
        """Freeze the live world for a checkpoint pointer.

        Fork the runner (a mutable object must be copied to stay reusable); the
        step vectors and ``best_trend`` are already immutable, so the returned
        value is a stable snapshot even as the live world keeps advancing.
        """
        from pyrung.core.analysis.pilot.overlay import fork_with_pilot_rungs

        return self.world.set(work=fork_with_pilot_rungs(self.world.work, self.pilot_rungs))

    def load_world(self, world: _World) -> None:
        """Revert: the checkpoint's world *is* the answer.

        Re-fork ``work`` so the checkpoint stays reusable for a repeat revert;
        ``committed_acts`` / ``best_trend`` / ``pilot_rungs`` restore by assignment.
        Rebuild the overlay explicitly on the fresh fork so the runner and the
        persistent world cannot disagree. No scan-cutoff reconstruction — the
        pointer already holds exactly the state that existed when the checkpoint
        was taken.
        """
        from pyrung.core.analysis.pilot.overlay import fork_with_pilot_rungs

        self.world = world.set(work=fork_with_pilot_rungs(world.work, world.pilot_rungs))


@dataclass(frozen=True)
class _IterationFrame:
    snap: dict[str, Any]
    tree: Any
    key: _StateKey
    distance_before: int
    raw_trace_actions: tuple[_ActionPair, ...]
    raw_trace_action_details: tuple[TraceAction, ...]


# ---------------------------------------------------------------------------
# Trial types (produced by steer, consumed by verify and pilot loop)
# ---------------------------------------------------------------------------


@dataclass
class _PulseState:
    """Physical execution evidence for one attempted act.

    Navigation's ``ChannelHeading`` is a declaration; the coast's actually
    selected landing and every trial observation live here, on the execution
    side, until VERIFY freezes what it accepts into ``_ExecutionEvidence``.
    """

    fork: PLC
    scan_before: int
    action_scan: int | None
    action_snap: dict[str, Any]
    wait_snaps: tuple[dict[str, Any], ...]
    post_pulse_snap: dict[str, Any]
    post_pulse_key: _StateKey
    snap: dict[str, Any]
    key: _StateKey
    # Ordered actual interpreter scans owned by this execution fork. Logical
    # scan IDs skipped by a fold are deliberately absent.
    kernel_scan_ids: tuple[int, ...]
    # Selected-scan replay memo. The immutable epoch owner is retained beside
    # each projection so a scan ID cannot reuse evidence from another fork.
    # ``init=False`` makes dataclasses.replace() start replay forks fresh.
    _projection_cache: dict[int, tuple[Any, ScanRungWriteProjection]] = field(
        default_factory=dict, init=False, repr=False
    )
    # Count actual ordered-projection replays, not cache reads. This is an
    # execution-local performance receipt used to prove that effects,
    # requirements, and intrascan share one already-run steer scan.
    _projection_replay_count: int = field(default=0, init=False, repr=False, compare=False)
    # The CoastReceipt of the trial's coast (bearing / terminal let-run), when the
    # trial had one — the recorded observation the deciders read instead of
    # re-deriving evidence from snapshots.  None for plain pulses.
    coast_receipt: CoastReceipt | None = None
    # The trial session's full event timeline (pen marks + trigger landings across
    # pulse, settle, and coast) — stamped onto the committed step context so
    # incident construction reads recorded evidence, not history re-diffs.
    timeline: tuple[CoastTriggerEvent, ...] = ()
    # A spin excursion may replace this trial with a replay-corrected fork.
    # Carry that exact correction with the fork so later gates cannot detach or
    # reconstruct the operation they are judging.
    confirmed_correction: _ConfirmedCorrection | None = None
    # Execution-owned channel selection. Navigation may declare a heading on
    # its ActPolicy, but only a physical coast can identify a terminal
    # departure or choose between an inner boundary and its outer route edge.
    channel_motion: ChannelMotion = field(default_factory=ChannelMotion)
    # Original operation boundary for causal replay.  Unlike
    # ``channel_motion``, this is never rebound to an ejection channel.
    replay_motion: ChannelMotion = field(default_factory=ChannelMotion)
    # Snapshot immediately before this act's first owned kernel scan.  This is
    # the execution-window entry value needed to distinguish a whole-window
    # zero-net excursion from ordinary non-zero channel motion without replaying
    # a projection merely to recover ``entry_tags``.
    source_snap: dict[str, Any] | None = None

    def __post_init__(self) -> None:
        if any(
            scan_id <= self.scan_before or scan_id > self.fork.state.scan_id
            for scan_id in self.kernel_scan_ids
        ):
            raise ValueError("kernel scan ID lies outside the trial execution window")
        if any(
            later <= earlier
            for earlier, later in zip(
                self.kernel_scan_ids,
                self.kernel_scan_ids[1:],
                strict=False,
            )
        ):
            raise ValueError("kernel scan IDs must be strictly increasing")

    def projection_at(self, scan_id: int) -> ScanRungWriteProjection | None:
        """Return one exact projection, memoized only for its epoch owner."""
        if scan_id not in self.kernel_scan_ids:
            return None
        owner = self.fork._causal_lineage.owner_at(scan_id)
        cached = self._projection_cache.get(scan_id)
        if cached is not None and cached[0] is owner:
            return cached[1]
        if owner is None:
            return None
        self._projection_replay_count += 1
        projection = self.fork._replay_pilot_rung_write_projection_at(scan_id)
        if projection is not None:
            self._projection_cache[scan_id] = (owner, projection)
        return projection

    @property
    def projection_replay_count(self) -> int:
        """Number of cache-miss projection replays owned by this execution."""

        return self._projection_replay_count

    def release_projections(self) -> None:
        """Release selected-scan replay evidence after its consumers finish."""
        self._projection_cache.clear()


@dataclass(frozen=True)
class _ExecutedAttempt:
    """One declared attempt paired with the exact physical evidence it produced."""

    pulse: _PulseState
    bearing: Bearing
    effect_observations: tuple[EffectObservation, ...] = ()
    # A factual, post-execution promise for the last selected-route value that
    # appeared before an off-route retained landing.  It complements rather
    # than replaces the bearing's immediate producer expectation.
    landing_expectation: EffectExpectation | None = None

    @property
    def assertion_scan(self) -> int:
        """Exact scan which asserted the selected act, or the physical landing."""

        if self.pulse.action_scan is not None:
            return self.pulse.action_scan
        if self.pulse.coast_receipt is not None:
            return self.pulse.coast_receipt.end_scan
        return self.pulse.fork.state.scan_id

    def projection_at(self, scan_id: int) -> ScanRungWriteProjection | None:
        """Reuse this execution's owner-bound projection cache."""

        project = getattr(self.pulse, "projection_at", None)
        return project(scan_id) if project is not None else None


@dataclass(frozen=True)
class TargetReached:
    """Verification accepted the fork because the user's target is true."""


@dataclass(frozen=True)
class AssessedMotion:
    """Verification accepted one classified non-target landing."""

    new_key: _StateKey
    trend: int
    assessment: TrialAssessment
    revisit_credentials: tuple[RevisitCredential, ...] = ()

    def __post_init__(self) -> None:
        if not self.assessment.accepted:
            raise ValueError("assessed motion requires an accepted assessment")


TrialVerification = TargetReached | AssessedMotion


@dataclass(frozen=True)
class _AcceptedTrial:
    """One accepted execution with verification's evidence and judgment.

    The executed attempt remains intact beside the frozen, PLC-free evidence
    VERIFY selected from its final pulse. Consumers read navigation declarations
    from ``attempt`` and durable observations from ``execution`` explicitly.
    """

    attempt: _ExecutedAttempt
    execution: _ExecutionEvidence
    verification: TrialVerification
    earned_work_receipt: EarnedWorkReceipt = field(default_factory=EarnedWorkReceipt)
    gate_events: tuple[PilotGateEvent, ...] = ()


@dataclass(frozen=True)
class _AttemptResult:
    trial: _AcceptedTrial | None
    # Exact disposable execution produced by the attempt, retained even when
    # verification rejects it.  The outer loop alone decides whether to apply
    # its observations/nogoods or commit its fork; bounded candidate composers
    # may instead orient that fork locally and discard it.
    executed: _ExecutedAttempt | None = None
    # A verification-time excursion is reported to the drive loop with the
    # exact execution that exhibited it.  PILOT owns the one investigation and
    # hands the result back to verification for the remaining gates.
    excursion_attempt: _ExecutedAttempt | None = None
    gate_events: tuple[PilotGateEvent, ...] = ()
    nogood_pairs: frozenset[_ActionPair] = frozenset()
    confirmed_correction: _ConfirmedCorrection | None = None
    # Compass observations gathered during the Act — applied only at the loop's
    # drive-loop application point (``_record_attempt``), never by the instrument itself.
    observations: tuple[CompassObservation, ...] = ()
    # Names of the ``avoid=`` conditions this trial tripped (action gate before
    # the pulse, or scan gate on a settled/transient snapshot).  Folded into
    # ``_PilotState.avoid_names`` when applied so a terminal decline can name what
    # excluded the path.
    avoid_names: tuple[str, ...] = ()
    # A stalled terminal let-run's receipt + pending-effects flag (trial=None,
    # nothing committed).  The loop reads these to decide whether the stall is
    # trustworthy memo material (quiescent) or must stay re-runnable (a timer
    # was mid-flight when the budget ran out).
    stall_receipt: Any = None
    stall_pending: bool = False
    # The rejection is backed by exact obligation/requirement evidence rather
    # than an empirical failed act.  It may establish a narrower requirement,
    # but it must never become an ActionNogood merely because it was rejected.
    proof_rejection: bool = False

    @property
    def executed_attempt(self) -> _ExecutedAttempt | None:
        """The one physical execution selected for all downstream evidence."""

        if self.trial is not None:
            return self.trial.attempt
        return self.executed or self.excursion_attempt

    def release_projections(self) -> None:
        """Release each physical execution's shared projection cache once."""

        executions = (
            self.executed,
            self.excursion_attempt,
            self.trial.attempt if self.trial is not None else None,
        )
        released: set[int] = set()
        for execution in executions:
            if execution is None or id(execution.pulse) in released:
                continue
            released.add(id(execution.pulse))
            execution.pulse.release_projections()
