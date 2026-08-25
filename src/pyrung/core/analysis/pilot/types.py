"""Cross-module protocols and state records for the PILOT package.

The records distinguish the revertible PLC world from search knowledge that
survives a revert, and carry iteration, trial, event, incident, and correction
data between reading, execution, verification, and recovery modules.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field, replace
from typing import TYPE_CHECKING, Any, Literal

from pyrsistent import PVector, pvector

import pyrung.core.analysis.pilot.world as _world
from pyrung.core.analysis.pilot.coast import CoastReceipt, CoastTriggerEvent
from pyrung.core.analysis.pilot.earned_work import EarnedWorkReceipt
from pyrung.core.analysis.pilot.execution import (
    ChannelMotion,
    ExecutionPoint,
    ExecutionReceipt,
    ScanEntryConfiguration,
    StopReceipt,
)
from pyrung.core.analysis.pilot.working_theory import (
    TheoryState,
    TheoryView,
)
from pyrung.core.analysis.pilot.world_key import _StateKey

if TYPE_CHECKING:
    from pyrung.core.analysis.causal._rung_writes import ScanRungWriteProjection
    from pyrung.core.analysis.pdg import ProgramGraph
    from pyrung.core.analysis.pilot.bootstrap import _BootstrapExecution
    from pyrung.core.analysis.pilot.compass import Compass, CompassObservation
    from pyrung.core.analysis.pilot.departure_state import PendingDeparture
    from pyrung.core.analysis.pilot.effects import (
        EffectExpectation,
        EffectObservation,
    )
    from pyrung.core.analysis.pilot.evidence import PipelineRoles, TransitionEvidence
    from pyrung.core.analysis.pilot.navigation_contracts import (
        ActPolicy,
        Bearing,
        EvidenceScope,
        TargetSpec,
        _ActionPair,
    )
    from pyrung.core.analysis.pilot.outcome import TrialAssessment
    from pyrung.core.analysis.pilot.overlay import PilotRung
    from pyrung.core.analysis.pilot.requirements import (
        ActiveRequirement,
        ExpectationReceipt,
        FailedEffectReceipt,
    )
    from pyrung.core.analysis.pilot.trace_read import DomainPrior, TraceChoice
    from pyrung.core.analysis.pilot.trace_tree import TraceAction
    from pyrung.core.analysis.pilot.world_key import _StateKeyConfig
    from pyrung.core.runner import PLC

# ---------------------------------------------------------------------------
# Type aliases
# ---------------------------------------------------------------------------


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
    # writer selection: a momentary command, never a hold.
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
    # The exact requirements introduced by the latest rejected attempt. The
    # broader temporal set above reconstructs the whole retry transaction;
    # this narrow set lets evidence readers extend only the new obstruction.
    temporal_trigger_requirements: tuple[ActiveRequirement, ...] = ()
    temporal_source_anchor: tuple[Any, Any] | None = None
    # True only while selecting the route used to bind an adjacent entry scan.
    # The result is invalidated immediately after binding, so exact per-input
    # ProgramStep receipts belong to the following fresh orientation.
    defer_program_input_receipts: bool = False


@dataclass(frozen=True)
class _StepContext:
    """Semantic and evidentiary context owned by one committed operation."""

    policy: ActPolicy
    execution: ExecutionReceipt
    settlement_execution: ExecutionReceipt | None = None
    frontier_tags: tuple[str, ...] = ()
    # Exact pilot rungs present during this step. Kept as ``Any`` here to
    # avoid coupling the state container back to the PilotRung implementation.
    pilot_rungs: tuple[Any, ...] = ()

    @property
    def steady_holds(self) -> tuple[str, ...]:
        """Concise view derived from the exact executable rung evidence."""
        return tuple(dict.fromkeys(rung.dest for rung in self.pilot_rungs))

    @property
    def execution_receipts(self) -> tuple[ExecutionReceipt, ...]:
        """Primary execution followed by its optional landing settlement."""

        return (
            (self.execution, self.settlement_execution)
            if self.settlement_execution is not None
            else (self.execution,)
        )

    def point_at(self, scan_id: int) -> ExecutionPoint | None:
        """Resolve one exact kernel scan without confusing logical fold gaps."""

        matches = tuple(
            point
            for receipt in self.execution_receipts
            if (point := receipt.point_at(scan_id)) is not None
        )
        if len(matches) > 1:
            raise RuntimeError("one operation receipt owns the same scan twice")
        return matches[0] if matches else None


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

    def point_at(self, scan_id: int) -> ExecutionPoint | None:
        """Resolve an exact scan only inside this act's retained logical prefix."""

        if not any(step.scan_before < scan_id <= step.scan_after for step in self.steps):
            return None
        return self.context.point_at(scan_id)


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


@dataclass
class _PilotState:
    # ── The world (reverts) ──
    # ``work`` / ``committed_acts`` / ``best_trend`` live inside a single
    # persistent :class:`_World` value. A checkpoint captures the whole world at
    # once and revert restores it by assignment (``snapshot_world`` /
    # ``load_world``). Flat steps are a read-only public/replay view derived from
    # the operation records below.
    world: _world._World
    # ── Knowledge (commits — never rolled back on revert) ──
    key_config: _StateKeyConfig | None
    seen_keys: set[_StateKey]
    checkpoints: list[_world._Checkpoint]
    watch_tags: list[str]
    # Exact executable boundary where this drive invocation began. Unlike the
    # optional bootstrap receipt, this exists at every scan number so a live
    # DAP/runner invocation can rebase an accepted early transaction.
    invocation_checkpoint: _world._CausalCheckpoint | None = None
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
    temporal_checkpoints: list[_world._CausalCheckpoint] = field(default_factory=list)
    # Immutable WorkingTheory knowledge. It is deliberately not part of
    # ``_World``: checkpoint restore must not erase observed lifecycle facts.
    # Some exact temporal facts now guide Compass, while optional lifecycle
    # recording remains isolated at its explicit adapter boundary.
    theory_state: TheoryState = field(default_factory=TheoryState)
    # Exact current-world acts rejected by obligation/requirement proof. These
    # are admissibility receipts, not empirical impossibility/nogood claims.
    # Their EvidenceScope includes the complete pre-action input context, so a
    # corrective level change admits the act anew while an unchanged replay
    # remains suppressed.
    proof_rejected_acts: set[tuple[EvidenceScope, tuple[Any, ...]]] = field(default_factory=set)
    # Invocation-local knowledge: reverting a handled transition must not
    # authorize the same source/action/evidence occurrence for another lap.
    consumed_revisits: set[RevisitCredential] = field(default_factory=set)
    # Physical scan where this PILOT invocation began. Search budgets are
    # relative to this anchor; accepted productive dwell is removed separately
    # by ``dwell_scans`` as the world advances and reverts.
    search_start_scan: int = 0
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

    def adopt_settlement(
        self,
        settled_work: PLC,
        settlement_execution: ExecutionReceipt,
    ) -> None:
        """Atomically adopt one operation's explicit settlement execution."""

        if not self.committed_acts:
            raise RuntimeError("settlement has no committed operation owner")
        act = self.committed_acts[-1]
        if act.context.settlement_execution is not None:
            raise RuntimeError("committed operation already owns a settlement")
        last = act.steps[-1]
        coast = settlement_execution.coast_receipt
        if coast is None or coast.kind != "departure-settle":
            raise ValueError("settlement execution requires a departure-settle receipt")
        if settlement_execution.source_scan != last.scan_after:
            raise ValueError("settlement source does not match its committed operation")
        if coast.start_scan != last.scan_after or coast.end_scan != settled_work.state.scan_id:
            raise ValueError("settlement receipt does not match its executable landing")
        final_step = replace(last, scan_after=coast.end_scan)
        final_act = replace(
            act,
            steps=(*act.steps[:-1], final_step),
            context=replace(act.context, settlement_execution=settlement_execution),
        )
        if self.journey and self.journey[-1] is last:
            self.journey[-1] = final_step
        committed = self.committed_acts.set(len(self.committed_acts) - 1, final_act)
        self.world = self.world.set(
            work=settled_work,
            committed_acts=committed,
            dwell_scans=self.dwell_scans + coast.logical_scans,
        )

    def assert_replay_tip(self) -> None:
        """Refuse to annex an unreceipted suffix when the target is observed."""

        if self.committed_acts and self.committed_acts[-1].steps[-1].scan_after != (
            self.work.state.scan_id
        ):
            raise RuntimeError("working World advanced beyond its committed replay tip")

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

    def snapshot_world(self) -> _world._World:
        """Freeze the live world for a checkpoint pointer.

        Fork the runner (a mutable object must be copied to stay reusable); the
        step vectors and ``best_trend`` are already immutable, so the returned
        value is a stable snapshot even as the live world keeps advancing.
        """
        from pyrung.core.analysis.pilot.overlay import fork_with_pilot_rungs

        return self.world.set(work=fork_with_pilot_rungs(self.world.work, self.pilot_rungs))

    def load_world(self, world: _world._World) -> None:
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
    side, until execution freezes it into an ``ExecutionReceipt`` for VERIFY.
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
    applied_configurations: tuple[ScanEntryConfiguration, ...] = ()
    stop_receipt: StopReceipt | None = None

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
    # Production executions freeze this before VERIFY judges the attempt.
    # Optional only for lightweight unit fixtures that construct this private
    # carrier without a real Epoch lineage.
    execution: ExecutionReceipt | None = None

    @property
    def assertion_scan(self) -> int:
        """Exact scan which asserted the selected act, or the physical landing."""

        if self.pulse.action_scan is not None and (
            not self.pulse.kernel_scan_ids or self.pulse.action_scan in self.pulse.kernel_scan_ids
        ):
            return self.pulse.action_scan
        if (
            self.pulse.coast_receipt is not None
            and self.pulse.coast_receipt.end_scan in self.pulse.kernel_scan_ids
        ):
            return self.pulse.coast_receipt.end_scan
        if self.pulse.kernel_scan_ids:
            # ProgramScan/ObserveScan own the exact kernel scan they selected.
            # VERIFY may inspect or settle the fork afterward; that later fork
            # tip is not the assertion occurrence and may have no projection
            # in this execution's bounded cache.
            return self.pulse.kernel_scan_ids[-1]
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

    VERIFY enriches the attempt's frozen receipt before acceptance. Consumers
    read navigation declarations from ``attempt`` and durable observations from
    its derived ``execution`` view; there is no second receipt owner.
    """

    attempt: _ExecutedAttempt
    verification: TrialVerification
    earned_work_receipt: EarnedWorkReceipt = field(default_factory=EarnedWorkReceipt)
    gate_events: tuple[PilotGateEvent, ...] = ()

    def __post_init__(self) -> None:
        if self.attempt.execution is None:
            raise ValueError("accepted trial requires an immutable execution receipt")

    @property
    def execution(self) -> ExecutionReceipt:
        execution = self.attempt.execution
        assert execution is not None  # enforced at construction
        return execution


@dataclass(frozen=True)
class _AttemptResult:
    trial: _AcceptedTrial | None
    # Exact disposable execution produced by the attempt, retained even when
    # verification rejects it. The outer loop alone decides whether to apply
    # its observations/nogoods or commit its fork.
    executed: _ExecutedAttempt | None = None
    # A verification-time excursion is reported to the drive loop with the
    # exact execution that exhibited it.  PILOT owns the one investigation and
    # hands the result back to verification for the remaining gates.
    excursion_attempt: _ExecutedAttempt | None = None
    gate_events: tuple[PilotGateEvent, ...] = ()
    nogood_pairs: frozenset[_ActionPair] = frozenset()
    # A replay-confirmed excursion may supply exact correction evidence,
    # but never an adopted replay World.  The requirement is inert until the
    # ordinary WorkingTheory/Compass loop composes and executes its PilotRungs.
    correction_requirement: ActiveRequirement | None = None
    # Compass observations gathered during the Act — applied only at the loop's
    # transition application point (``record_attempt``), never by the instrument itself.
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

    @property
    def execution_receipt(self) -> ExecutionReceipt | None:
        """The immutable receipt for the selected physical execution."""

        if self.trial is not None:
            return self.trial.execution
        executed = self.executed_attempt
        return executed.execution if executed is not None else None

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
