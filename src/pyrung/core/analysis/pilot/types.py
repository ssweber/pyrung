"""Shared types for the PILOT package.

Cross-boundary dataclasses and type aliases imported by pilot.py, verify.py,
steer.py, candidates.py, and progress.py.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from enum import Enum
from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

from pyrsistent import PRecord, PVector, pvector
from pyrsistent import field as _precord_field

if TYPE_CHECKING:
    from pyrung.core.analysis.pdg import ProgramGraph
    from pyrung.core.analysis.pilot._ops import _StateKeyConfig
    from pyrung.core.analysis.pilot.compass import Compass, CompassObservation
    from pyrung.core.analysis.pilot.evidence import PipelineRoles, TransitionEvidence
    from pyrung.core.analysis.pilot.outcome import Outcome, TrialAssessment
    from pyrung.core.analysis.pilot.trace import DomainPrior, TraceAction, TraceChoice
    from pyrung.core.runner import PLC

# ---------------------------------------------------------------------------
# Type aliases
# ---------------------------------------------------------------------------

_ActionPair = tuple[str, Any]
_StateKey = tuple[Any, ...]
_ObserveFn = Callable[[str, dict[str, Any], Any], None]


class MotionKind(Enum):
    """Execution semantics of a trial; event labels remain diagnostic only."""

    INTERVENTION = "intervention"
    COAST_TO_BEARING = "coast-to-bearing"
    COAST_HOLDING_WORLD = "coast-holding-world"

    @property
    def is_coast(self) -> bool:
        return self is not MotionKind.INTERVENTION


# ---------------------------------------------------------------------------
# WalkContext — the read-side seam of a backward trace
# ---------------------------------------------------------------------------


@runtime_checkable
class WalkContext(Protocol):
    """What a read-side capability may consume from one backward trace.

    ``trace.py``'s ``_TraceEnv`` bundles *all* the constants threaded through one
    trace — but a **read-side capability** (a static reader that resolves a need by
    *reading the charts*, never running the ship) consumes only the world-describing
    subset, never the route/recursion control (writer/or locks, ``avoid_pred`` /
    ``via_pred``, ``guard_memo``, ``max_depth``, ``harness``, ``clear_only``).  That
    subset — the seam — is this structural protocol:

    - ``snapshot`` — the live register frame guards are evaluated against;
    - ``pdg`` — the :class:`ProgramGraph` (``writers_of`` / ``rung_nodes`` / ``tags``);
    - ``program`` — the Program (``resolve_rung``, instruction bodies);
    - ``steerable`` — the free input tags a reader may treat as available levers;
    - ``opaque_loop`` — the pinned pipeline registers a reader folds into its own
      ``current_tags`` (``{target} | opaque_loop``);
    - ``prior`` — the prover-derived ``DomainPrior`` (``nd_domains`` / ``func_deps``)
      an *enumerating* reader needs for complete-domain soundness.

    ``_TraceEnv`` satisfies this **structurally** (it carries these six as
    attributes), so ``trace.py`` passes its ``env`` straight in with no adapter.  The
    seam lives here — importable **without** ``trace`` — precisely so the next
    read-side instrument is *born in its own module* consuming a ``WalkContext``, and
    ``trace.py`` imports it, rather than being written inside ``trace.py`` because the
    walk context was trace-private (``availability.py`` is the worked example of that
    born-inside-then-extracted anti-pattern).  See ``pilot/CLAUDE.md`` — "Where new
    read-side capabilities live".
    """

    snapshot: Mapping[str, Any]
    pdg: ProgramGraph
    program: Any
    steerable: frozenset[str]
    opaque_loop: frozenset[str]
    prior: DomainPrior | None


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
    """One avoided condition, carrying its own printable name for declines."""

    name: str
    pred: Callable[[dict[str, Any]], bool]
    tags: frozenset[str] = frozenset()


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
    persistent: the ``steps`` / ``step_contexts`` PVectors are immutable, so once
    a checkpoint captures a world (``snapshot_world``) later appends build a fresh
    world value and never mutate the captured one — the pointer semantics revert
    relies on.

    ``work`` is a live runner fork (a mutable object); the world merely holds a
    *reference* to it.  The persistence lives in the structure around it — the
    step vectors and ``best_trend`` — which is exactly the bookkeeping the old
    scan-cutoff filtering hand-reconstructed on revert.
    """

    work = _precord_field()
    steps = _precord_field()
    step_contexts = _precord_field()
    best_trend = _precord_field()
    rungs = _precord_field()
    # Committed scan-ids spent *waiting* — the spans of accepted zoom / let-run
    # coasts.  Timer dwell is waiting, not searching (see ``_ops._ZOOM_BUDGET``),
    # so the loop budget charges ``scan_id - dwell_scans``: an accepted coast
    # that rides a 39k-scan dwell must not bankrupt the search.  Lives in the
    # world so a revert rewinds the credit together with the scans it excused.
    dwell_scans = _precord_field()


@dataclass(frozen=True)
class _Checkpoint:
    """A revert anchor: a *pointer* to a world value plus the facts the launch knew.

    ``world`` is the immutable :class:`_World` captured at creation
    (``_PilotState.snapshot_world``); revert is ``state.load_world(cp.world)`` —
    plain assignment, not a scan-cutoff reconstruction.

    ``frontier`` is the launching frame's outstanding non-steerable
    prerequisites (``trace.frontier_pairs``) captured at creation — the coast
    frame that later regresses has an empty tree, so investigation reads the
    frontier *here*, never re-derives it (``hold_defeats_needed``'s ``needed``).
    """

    key: _StateKey
    world: _World
    trend: int
    frontier: tuple[_ActionPair, ...] = ()


# ---------------------------------------------------------------------------
# Recorded step
# ---------------------------------------------------------------------------


@dataclass
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
    changed_tags: tuple[str, ...]
    departures: tuple[BearingDeparture, ...]
    # The macro-state register whose departure IS the incident (the zoom /
    # terminal-letrun channel tag) — other departures downstream of it are
    # collateral.  Hypothesis ranking keys causal primacy off its cause chain.
    channel_tag: str | None = None
    # The recorded session events inside the window (BumpEvents, ordered,
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
    target_tag: str
    target_value: Any
    # Live relational target predicate (``A op B`` Atom) when how() was given a
    # comparison; None for Tag / equality targets.  When set, the drive loop
    # traces it via trace_relational and judges "reached" by evaluating the
    # predicate (target_reached), not equality on (target_tag, target_value).
    target_predicate: Any
    pdg: ProgramGraph
    program: Any
    steerable: frozenset[str]
    edge_tags: set[str]
    resting: dict[str, Any]
    nd_domains: dict[str, tuple[Any, ...]] | None
    domain_prior: DomainPrior | None
    evidence: TransitionEvidence | None
    # A compass *value*, replaced once per attempt / skiff round at the loop's
    # RECORD point (``ctx.compass, _ = ctx.compass.apply(...)``) — never a shared
    # mutable advanced behind readers' backs.  Knowledge commits, the world
    # reverts.
    compass: Compass
    opaque_loop: frozenset[str]
    pipeline_roles: tuple[PipelineRoles, ...]
    pipeline_internal_tags: frozenset[str]
    # The locked default route through a multi-route Bool trace (a TraceChoice),
    # or None.  Picked by ``_prepare_route``; reported to the user as
    # ``Path.route`` (a RouteTaken).
    route: TraceChoice | None
    blocked_route_actions: frozenset[_ActionPair]
    max_scans: int
    live: bool
    debug: bool
    avoid_pred: Any = None
    via_pred: Any = None
    # Clear-only (ack-cleared momentary) command tags — the pulse-treatment set.
    # Kept off prerequisite holds (candidates.py) and off preferred init/reset
    # writer selection (trace._rank_writers): a momentary command, never a hold.
    clear_only: frozenset[str] = frozenset()

    def route_allowed(self, pair: _ActionPair) -> bool:
        return pair not in self.blocked_route_actions


@dataclass
class _StepContext:
    """Metadata captured at commit time for a trial — truncated alongside ``steps``."""

    scan_before: int
    observe_label: str
    candidate: dict[str, Any]
    motion: MotionKind = MotionKind.INTERVENTION
    frontier_tags: tuple[str, ...] = ()
    steady_holds: tuple[str, ...] = ()
    pulsing_holds: tuple[str, ...] = ()
    channel_tag: str | None = None
    before_snap: dict[str, Any] = field(default_factory=dict)
    after_snap: dict[str, Any] = field(default_factory=dict)
    # The requested channel value for a coast step (the zoom bearing / the
    # letrun channel's held value) — with ``motion`` + ``channel_tag`` this is
    # the recorded session spec a replay re-arms, replacing the old positional
    # "last empty-input step is the eject coast" inference.
    channel_target: Any = None
    # The step's session timeline (pen marks + bump landings).  Incident
    # construction reads these instead of re-diffing history windows.
    timeline: tuple[Any, ...] = ()


@dataclass(frozen=True)
class _HoldLogEntry:
    """One hold installation event — append-only, survives reverts."""

    scan: int
    tags: tuple[_ActionPair, ...]
    source: str


@dataclass
class _PilotState:
    # ── The world (reverts) ──
    # ``work`` / ``steps`` / ``step_contexts`` / ``best_trend`` live inside a
    # single persistent :class:`_World` value.  They are still read and written
    # by their bare names through the properties below (``state.work``,
    # ``state.steps``, …), so callers never touch ``.world`` directly — but a
    # checkpoint captures the whole world at once and revert restores it by
    # assignment (``snapshot_world`` / ``load_world``).
    world: _World
    # ── Knowledge (commits — never rolled back on revert) ──
    key_config: _StateKeyConfig | None
    seen_keys: set[_StateKey]
    nogoods: dict[_StateKey, set[_ActionPair]]
    checkpoints: list[_Checkpoint]
    watch_tags: list[str]
    expanded_tags: set[str] = field(default_factory=set)
    last_wait_log: tuple[Any, ...] | None = None
    # The target-relative progress gauge (gauge.py) — event-earned
    # ordinals the threshold-masked search key aliases.  Static knowledge,
    # built once at loop init; a None/empty gauge degrades consumers (verify
    # spin/cycle gates, departure classification) to key-only behavior.
    gauge: Any = None
    # A bounded provisional departure. It is promoted when the target-relative
    # gauge advances, regressed only when that gauge moves behind, and otherwise
    # rolled back on expiry without manufacturing a nogood.
    provisional: Any = None
    # World key -> stop_reason of the terminal let-run that already ran there.
    # A receipt-backed memo (the audit's C2): an entry is recorded only when it
    # can be trusted — the coast ejected (deterministic re-eject after a revert
    # restores the identical world), or it stalled *quiescent* (no pending
    # harness feedback / running accumulator, so the masked state key genuinely
    # captures the world).  A stall with pending effects is deliberately NOT
    # memoized: the key masks accumulators, and a same-key world with a timer
    # mid-flight might complete where this one timed out.  The world key
    # already includes the rung overlay, so a newly installed hold re-fires
    # the let-run naturally.
    letrun_memo: dict[_StateKey, str] = field(default_factory=dict)
    # State key -> number of skiff (ORIENT last-tier) escalations spent there.  The
    # skiff is the reading-ladder's last tier; a stuck key gets a bounded number of
    # skiff laps and then the loop STOPS honestly instead of alternating forever.
    # Knowledge: the world reverts between laps but this does not, so re-arriving
    # stuck at the same key is recognized as "the skiff's probe-mark churn is not
    # moving the world" (Legibility — a stall you can dump and point at).  Owned by
    # ``_orient_escalate_skiff`` (the escalation table's skiff row).
    stuck_keys: dict[_StateKey, int] = field(default_factory=dict)
    # Append-only log of every committed step, including attempts later reverted.
    # ``steps`` (the world) is the clean, sequentially-replayable path (restored
    # to the checkpoint's on revert); ``journey`` keeps the full "tried this,
    # ejected, learned, retried" record surfaced by ``how(..., debug=True)``.
    journey: list[_Step] = field(default_factory=list)
    hold_log: list[_HoldLogEntry] = field(default_factory=list)
    # Named honest-decline reasons the skiff produced when it met an unreadable
    # frontier gated by a free word with no declared complete domain.  This is
    # committed Knowledge (an experiment survives rollback), but it is scoped by
    # executable world key: a decline learned in world A cannot explain a stop in
    # world B.  The terminal Plan exposes only the entry applicable to its final
    # world through the legacy singular ``Plan.skiff_decline`` field.
    skiff_declines: dict[_StateKey, str] = field(default_factory=dict)
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
    def steps(self) -> PVector[_Step]:
        return self.world.steps

    @steps.setter
    def steps(self, value: Any) -> None:
        self.world = self.world.set(steps=pvector(value))

    @property
    def step_contexts(self) -> PVector[_StepContext]:
        return self.world.step_contexts

    @step_contexts.setter
    def step_contexts(self, value: Any) -> None:
        self.world = self.world.set(step_contexts=pvector(value))

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

    def snapshot_world(self) -> _World:
        """Freeze the live world for a checkpoint pointer.

        Fork the runner (a mutable object must be copied to stay reusable); the
        step vectors and ``best_trend`` are already immutable, so the returned
        value is a stable snapshot even as the live world keeps advancing.
        """
        return self.world.set(work=self.world.work.fork())

    def load_world(self, world: _World) -> None:
        """Revert: the checkpoint's world *is* the answer.

        Re-fork ``work`` so the checkpoint stays reusable for a repeat revert;
        ``steps`` / ``step_contexts`` / ``best_trend`` / ``rungs`` restore by
        assignment.  Rebuild the overlay explicitly on the fresh fork so the
        runner and the persistent world cannot disagree.  No scan-cutoff
        reconstruction — the pointer already holds exactly the state that
        existed when the checkpoint was taken.
        """
        self.world = world.set(work=world.work.fork())
        from pyrung.core.analysis.pilot._ops import _set_rungs

        _set_rungs(self.work, list(self.rungs))


@dataclass(frozen=True)
class _IterationFrame:
    snap: dict[str, Any]
    tree: Any
    key: _StateKey
    distance_before: int
    raw_trace_actions: tuple[_ActionPair, ...]
    raw_trace_action_details: tuple[TraceAction, ...]
    # The completion re-read's unmet frontier (candidates.py), stamped by the
    # loop after ORIENT so ``_frontier_clause`` names the pressable lever behind
    # a prescribed wait (``x_RotateFB``) instead of the target tree's post-cut
    # interior.  Empty unless this iteration prescribed a wait with completion.
    completion_frontier: tuple[_ActionPair, ...] = ()


# ---------------------------------------------------------------------------
# Trial types (produced by steer, consumed by verify and pilot loop)
# ---------------------------------------------------------------------------


@dataclass
class _PulseState:
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
    coast_receipt: Any = None
    # The trial session's full event timeline (pen marks + bump landings across
    # pulse, settle, and coast) — stamped onto the committed step context so
    # incident construction reads recorded evidence, not history re-diffs.
    timeline: tuple[Any, ...] = ()


@dataclass(frozen=True)
class _TrialResult:
    fork: PLC
    scan_before: int
    # The narrow candidate choice (e.g. ``{C_Start: True}``) — what to record on
    # the recorded step is ``applied`` (the full set including co-actions), not
    # this.  See ``_Step.inputs``.
    candidate: dict[str, Any]
    applied: tuple[_ActionPair, ...]
    before_snap: dict[str, Any]
    post_pulse_snap: dict[str, Any]
    fork_snap: dict[str, Any]
    observe_label: str
    motion: MotionKind = MotionKind.INTERVENTION
    new_key: _StateKey | None = None
    trend: int | None = None
    outcome: Outcome | None = None
    assessment: TrialAssessment | None = None
    # The post-trial tree's non-steerable frontier (trace.frontier_pairs over the
    # dead-end gate's tree) — captured on the checkpoint this trial may create.
    frontier: tuple[_ActionPair, ...] = ()
    regression_nogoods: frozenset[_ActionPair] = frozenset()
    chase_regression_causes: bool = True
    gate_events: tuple[PilotGateEvent, ...] = ()
    zoom_channel_tag: str | None = None
    zoom_target_value: Any = None
    # See _PulseState.coast_receipt — carried through verify onto the result.
    coast_receipt: Any = None
    # See _PulseState.timeline — carried through verify onto the result.
    timeline: tuple[Any, ...] = ()


@dataclass(frozen=True)
class _AttemptResult:
    trial: _TrialResult | None
    gate_events: tuple[PilotGateEvent, ...] = ()
    nogood_pairs: frozenset[_ActionPair] = frozenset()
    excursion_holds: tuple[_ActionPair, ...] = ()
    # Compass observations gathered during the Act — applied only at the loop's
    # RECORD point (``_record_attempt``), never by the instrument itself.
    observations: tuple[CompassObservation, ...] = ()
    # Names of the ``avoid=`` conditions this trial tripped (action gate before
    # the pulse, or scan gate on a settled/transient snapshot).  Folded into
    # ``_PilotState.avoid_names`` at RECORD so a terminal decline can name what
    # excluded the path.
    avoid_names: tuple[str, ...] = ()
    # A stalled terminal let-run's receipt + pending-effects flag (trial=None,
    # nothing committed).  The loop reads these to decide whether the stall is
    # trustworthy memo material (quiescent) or must stay re-runnable (a timer
    # was mid-flight when the budget ran out).
    stall_receipt: Any = None
    stall_pending: bool = False
