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
from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

from pyrsistent import PRecord, PVector, pvector
from pyrsistent import field as _precord_field

from pyrung.core.analysis.pilot.coast import CoastReceipt, CoastTriggerEvent
from pyrung.core.analysis.pilot.earned_work import EarnedWorkReceipt

if TYPE_CHECKING:
    from pyrung.core.analysis.pdg import ProgramGraph
    from pyrung.core.analysis.pilot._ops import PilotRung, _StateKeyConfig
    from pyrung.core.analysis.pilot.compass import Compass, CompassObservation
    from pyrung.core.analysis.pilot.evidence import PipelineRoles, TransitionEvidence
    from pyrung.core.analysis.pilot.navigation_contracts import (
        ActPolicy,
        Bearing,
        BearingObjective,
        TargetSpec,
    )
    from pyrung.core.analysis.pilot.outcome import TrialAssessment
    from pyrung.core.analysis.pilot.progress import PendingDeparture
    from pyrung.core.analysis.pilot.trace import DomainPrior, TraceAction, TraceChoice
    from pyrung.core.runner import PLC

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

    snapshot: Mapping[str, Any]
    pdg: ProgramGraph
    program: Any
    steerable: frozenset[str]
    opaque_loop: frozenset[str]
    prior: DomainPrior | None


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
    journey, …) survives.  Pilot rungs belong here: they change what the next
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
    rungs = _precord_field()
    # Committed scan-ids spent *waiting* — the spans of accepted zoom / let-run
    # coasts.  Timer dwell is waiting, not searching (see ``_ops._ZOOM_BUDGET``),
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


class DepartureAction(Enum):
    """What progress policy should do with an unresolved departure."""

    WAIT = "wait"
    PROMOTE = "promote"
    REGRESS = "regress"
    EXPIRE = "expire"


class DepartureBasis(Enum):
    """Exceptional policy evidence applied without rewriting earned-work facts."""

    PILOT_CAUSED_REGRESSION = "pilot_caused_regression"


@dataclass(frozen=True)
class DepartureDecision:
    """One evidence-based assessment of a pending departure."""

    action: DepartureAction
    receipt: EarnedWorkReceipt
    basis: DepartureBasis | None = None


# ---------------------------------------------------------------------------
# Recorded step
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _Step:
    # The inputs physically applied for this step — the candidate plus its
    # co-actions (command button + one-shot edge gate), i.e. ``trial.applied``,
    # NOT the narrow ``trial.candidate``.  Named ``inputs`` (matching the prover's
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
    # The macro-state register whose departure IS the incident (the zoom /
    # terminal-letrun channel tag) — other departures downstream of it are
    # collateral.  Hypothesis ranking keys causal primacy off its cause chain.
    channel_tag: str | None = None
    # The recorded session events inside the window (CoastTriggerEvents, ordered,
    # same-scan groups preserved).  This is the incident's evidence: a
    # fire-then-reset pulse is two transitions here, never a net no-op.
    timeline: tuple[Any, ...] = ()


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
    live: bool
    key_config: _StateKeyConfig | None = None
    avoid_pred: Any = None
    # Clear-only (ack-cleared momentary) command tags — the pulse-treatment set.
    # Kept off prerequisite holds (options.py) and off preferred init/reset
    # writer selection (trace._rank_writers): a momentary command, never a hold.
    clear_only: frozenset[str] = frozenset()


@dataclass(frozen=True)
class _ExecutionEvidence:
    """PLC-free evidence from the final execution VERIFY accepted.

    Navigation's policy remains a declaration. This record contains only the
    final observed operation window, copied away from the mutable execution
    fork after any retry has selected the pulse that will be committed.
    """

    before_snap: Mapping[str, Any]
    after_snap: Mapping[str, Any]
    channel_motion: ChannelMotion
    coast_receipt: CoastReceipt | None
    timeline: tuple[CoastTriggerEvent, ...]

    def __post_init__(self) -> None:
        object.__setattr__(self, "before_snap", MappingProxyType(dict(self.before_snap)))
        object.__setattr__(self, "after_snap", MappingProxyType(dict(self.after_snap)))
        object.__setattr__(self, "timeline", tuple(self.timeline))

    @property
    def accelerators(self) -> tuple[_ActionPair, ...]:
        """Exact CycleFold writes derived from the typed coast receipt."""
        return self.coast_receipt.advances if self.coast_receipt is not None else ()


@dataclass(frozen=True)
class _StepContext:
    """Semantic and evidentiary context owned by one committed operation."""

    policy: ActPolicy
    execution: _ExecutionEvidence
    frontier_tags: tuple[str, ...] = ()
    # Exact synthesis rules present during this step. Kept as ``Any`` here to
    # avoid coupling the state container back to the PilotRung implementation.
    control_rungs: tuple[Any, ...] = ()

    @property
    def candidate(self) -> dict[str, Any]:
        return dict(self.policy.action_pairs)

    @property
    def motion(self) -> MotionKind:
        return self.policy.motion

    @property
    def channel_tag(self) -> str | None:
        return self.execution.channel_motion.channel_tag

    @property
    def channel_target(self) -> Any:
        return self.execution.channel_motion.target_value

    @property
    def before_snap(self) -> Mapping[str, Any]:
        return self.execution.before_snap

    @property
    def after_snap(self) -> Mapping[str, Any]:
        return self.execution.after_snap

    @property
    def timeline(self) -> tuple[CoastTriggerEvent, ...]:
        return self.execution.timeline

    @property
    def accelerators(self) -> tuple[_ActionPair, ...]:
        return self.execution.accelerators

    @property
    def steady_holds(self) -> tuple[str, ...]:
        """Concise view derived from the exact executable rung evidence."""
        return tuple(dict.fromkeys(rung.dest for rung in self.control_rungs))


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
    rungs: tuple[PilotRung, ...]

    @property
    def tags(self) -> tuple[_ActionPair, ...]:
        """Concise recording view derived from the installed executable form."""
        return tuple((rung.dest, rung.value) for rung in self.rungs)


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

    @property
    def identity(self) -> tuple[tuple[Any, ...], ...]:
        return self.correction.identity

    @property
    def rungs(self) -> tuple[PilotRung, ...]:
        return self.correction.rungs

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
    rungs: tuple[PilotRung, ...]
    sources: tuple[str, ...]
    justification: str


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
    def rungs(self) -> PVector[Any]:
        return self.world.rungs

    @rungs.setter
    def rungs(self, value: Any) -> None:
        self.world = self.world.set(rungs=pvector(value))

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
        from pyrung.core.analysis.pilot._ops import fork_with_rungs

        return self.world.set(work=fork_with_rungs(self.world.work, self.rungs))

    def load_world(self, world: _World) -> None:
        """Revert: the checkpoint's world *is* the answer.

        Re-fork ``work`` so the checkpoint stays reusable for a repeat revert;
        ``committed_acts`` / ``best_trend`` / ``rungs`` restore by assignment.
        Rebuild the overlay explicitly on the fresh fork so the runner and the
        persistent world cannot disagree. No scan-cutoff reconstruction — the
        pointer already holds exactly the state that existed when the checkpoint
        was taken.
        """
        from pyrung.core.analysis.pilot._ops import fork_with_rungs

        self.world = world.set(work=fork_with_rungs(world.work, world.rungs))


@dataclass(frozen=True)
class _IterationFrame:
    snap: dict[str, Any]
    tree: Any
    key: _StateKey
    distance_before: int
    raw_trace_actions: tuple[_ActionPair, ...]
    raw_trace_action_details: tuple[TraceAction, ...]
    # The completion re-read's unmet frontier (options.py), stamped by the
    # orientation owner after candidate reading so ``_frontier_clause`` names
    # the pressable lever behind a prescribed wait (``x_RotateFB``) instead of
    # the target tree's post-cut interior. Empty unless this iteration prescribed
    # a wait with completion.
    completion_frontier: tuple[_ActionPair, ...] = ()


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
    action_scan: int
    action_snap: dict[str, Any]
    wait_snaps: tuple[dict[str, Any], ...]
    post_pulse_snap: dict[str, Any]
    post_pulse_key: _StateKey
    snap: dict[str, Any]
    key: _StateKey
    # The CoastReceipt of the trial's coast (zoom / terminal let-run), when the
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


@dataclass(frozen=True)
class _ExecutedAttempt:
    """One declared attempt paired with the exact physical evidence it produced."""

    pulse: _PulseState
    bearing: Bearing


@dataclass(frozen=True)
class TargetReached:
    """Verification accepted the fork because the user's target is true."""


@dataclass(frozen=True)
class AssessedMotion:
    """Verification accepted one classified non-target landing."""

    new_key: _StateKey
    trend: int
    assessment: TrialAssessment

    def __post_init__(self) -> None:
        if not self.assessment.accepted:
            raise ValueError("assessed motion requires an accepted assessment")


TrialVerification = TargetReached | AssessedMotion


@dataclass(frozen=True)
class _AcceptedTrial:
    """One accepted execution with verification's evidence and judgment.

    The executed attempt remains intact beside the frozen, PLC-free evidence
    VERIFY selected from its final pulse. Compatibility views below derive
    navigation declarations from the attempt's policy and durable observations
    from that execution evidence.
    """

    attempt: _ExecutedAttempt
    execution: _ExecutionEvidence
    verification: TrialVerification
    earned_work_receipt: EarnedWorkReceipt = field(default_factory=EarnedWorkReceipt)
    gate_events: tuple[PilotGateEvent, ...] = ()

    @property
    def pulse(self) -> _PulseState:
        return self.attempt.pulse

    @property
    def bearing(self) -> Bearing:
        return self.attempt.bearing

    @property
    def policy(self) -> ActPolicy:
        return self.bearing.act.policy

    @property
    def fork(self) -> PLC:
        return self.pulse.fork

    @property
    def scan_before(self) -> int:
        return self.pulse.scan_before

    @property
    def candidate(self) -> dict[str, Any]:
        return dict(self.policy.action_pairs)

    @property
    def applied(self) -> tuple[_ActionPair, ...]:
        return self.policy.applied

    @property
    def before_snap(self) -> Mapping[str, Any]:
        return self.execution.before_snap

    @property
    def post_pulse_snap(self) -> dict[str, Any]:
        return self.pulse.post_pulse_snap

    @property
    def fork_snap(self) -> Mapping[str, Any]:
        return self.execution.after_snap

    @property
    def channel_motion(self) -> ChannelMotion:
        return self.execution.channel_motion

    @property
    def observe_label(self) -> str:
        if isinstance(self.verification, TargetReached):
            return self.policy.target_observe_label
        return self.policy.observe_label

    @property
    def bearing_objective(self) -> BearingObjective:
        return self.bearing.objective

    @property
    def route_prescribed(self) -> bool:
        return self.policy.route_prescribed

    @property
    def motion(self) -> MotionKind:
        return self.policy.motion

    @property
    def regression_nogoods(self) -> frozenset[_ActionPair]:
        return self.policy.regression_nogoods

    @property
    def chase_regression_causes(self) -> bool:
        return self.policy.chase_regression_causes

    @property
    def coast_receipt(self) -> CoastReceipt | None:
        return self.execution.coast_receipt

    @property
    def timeline(self) -> tuple[CoastTriggerEvent, ...]:
        return self.execution.timeline


@dataclass(frozen=True)
class _AttemptResult:
    trial: _AcceptedTrial | None
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
