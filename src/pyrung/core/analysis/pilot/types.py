"""Shared types for the PILOT package.

Cross-boundary dataclasses and type aliases imported by pilot.py, verify.py,
steer.py, candidates.py, and progress.py.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from pyrung.core.analysis.pdg import ProgramGraph
    from pyrung.core.analysis.pilot._ops import _StateKeyConfig
    from pyrung.core.analysis.pilot.compass import Compass, CompassObservation
    from pyrung.core.analysis.pilot.evidence import PipelineRoles, TransitionEvidence
    from pyrung.core.analysis.pilot.outcome import Outcome
    from pyrung.core.analysis.pilot.trace import DomainPrior, TraceAction, TraceChoice
    from pyrung.core.runner import PLC

# ---------------------------------------------------------------------------
# Type aliases
# ---------------------------------------------------------------------------

_ActionPair = tuple[str, Any]
_StateKey = tuple[Any, ...]
_Checkpoint = tuple[_StateKey, Any, int]
_ObserveFn = Callable[[str, dict[str, Any], Any], None]


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

    def route_allowed(self, pair: _ActionPair) -> bool:
        return pair not in self.blocked_route_actions


@dataclass
class _StepContext:
    """Metadata captured at commit time for a trial — truncated alongside ``steps``."""

    scan_before: int
    observe_label: str
    candidate: dict[str, Any]
    frontier_tags: tuple[str, ...] = ()
    steady_holds: tuple[str, ...] = ()
    pulsing_holds: tuple[str, ...] = ()
    governing_tag: str | None = None
    before_snap: dict[str, Any] = field(default_factory=dict)
    after_snap: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class _HoldLogEntry:
    """One hold installation event — append-only, survives reverts."""

    scan: int
    tags: tuple[_ActionPair, ...]
    source: str


@dataclass
class _PilotState:
    work: PLC
    key_config: _StateKeyConfig | None
    seen_keys: set[_StateKey]
    nogoods: dict[_StateKey, set[_ActionPair]]
    checkpoints: list[_Checkpoint]
    forced_holds: dict[str, Any]
    steps: list[_Step]
    watch_tags: list[str]
    expanded_tags: set[str] = field(default_factory=set)
    best_trend: int | None = None
    last_wait_log: tuple[Any, ...] | None = None
    # State key -> forced-hold count when the terminal let-run last ran there.
    # The coast is deterministic given the held inputs, so re-running at the same
    # key with no new hold just re-burns the budget (or re-ejects forever).  Only
    # re-fire when investigation has since installed a new hold (count grew).
    letrun_tried: dict[_StateKey, int] = field(default_factory=dict)
    # Append-only log of every committed step, including attempts later reverted.
    # ``steps`` is the clean, sequentially-replayable path (truncated on revert);
    # ``journey`` keeps the full "tried this, ejected, learned, retried" record
    # surfaced by ``how(..., debug=True)``.
    journey: list[_Step] = field(default_factory=list)
    step_contexts: list[_StepContext] = field(default_factory=list)
    hold_log: list[_HoldLogEntry] = field(default_factory=list)
    # A named honest-decline reason the skiff produced when it met an unreadable
    # frontier gated by a free word with no declared complete domain — nothing to
    # probe soundly.  Set by ``probe_live_guard_frontiers``; the terminal stuck
    # exit prefers it over the generic ``stuck: <reason>`` so the miss names the
    # tag and nudges a ``choices=`` declaration.
    skiff_decline: str | None = None

    def revert_to(self, cp_fork: PLC) -> None:
        """Revert the work fork to a checkpoint and drop the abandoned steps.

        Steps committed at/after the checkpoint scan belong to the attempt being
        reverted: they leave ``steps`` (so the path stays sequentially replayable)
        but remain in ``journey`` (the full attempt log).  The exact cutoff is the
        one ``progress.build_replay_fn`` already uses for its investigation replay
        (``scan_before >= cp_fork.scan_id``).
        """
        self.work = cp_fork.fork()
        cutoff = cp_fork.state.scan_id
        self.steps = [s for s in self.steps if s.scan_before < cutoff]
        self.step_contexts = [c for c in self.step_contexts if c.scan_before < cutoff]


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
    fork: PLC
    scan_before: int
    action_scan: int
    action_snap: dict[str, Any]
    wait_snaps: tuple[dict[str, Any], ...]
    post_pulse_snap: dict[str, Any]
    post_pulse_key: _StateKey
    snap: dict[str, Any]
    key: _StateKey


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
    new_key: _StateKey | None = None
    trend: int | None = None
    outcome: Outcome | None = None
    regression_nogoods: frozenset[_ActionPair] = frozenset()
    chase_regression_causes: bool = True
    gate_events: tuple[PilotGateEvent, ...] = ()
    zoom_governing_tag: str | None = None
    zoom_target_value: Any = None


@dataclass(frozen=True)
class _AttemptResult:
    trial: _TrialResult | None
    gate_events: tuple[PilotGateEvent, ...] = ()
    nogood_pairs: frozenset[_ActionPair] = frozenset()
    excursion_holds: tuple[_ActionPair, ...] = ()
    # Compass observations gathered during the Act — applied only at the loop's
    # RECORD point (``_record_attempt``), never by the instrument itself.
    observations: tuple[CompassObservation, ...] = ()
