"""Shared immutable navigation contracts for PILOT.

The types in this module are deliberately free of reading and execution
algorithms.  ``Compass`` produces these values, ``pilot.py`` dispatches them,
and ``steer.py`` / ``skiff.py`` execute the declared work.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING, Any, Literal

from pyrung.core.analysis.pilot.types import MotionKind, _ActionPair, _StateKey
from pyrung.core.analysis.pilot.world_key import _semantic_key
from pyrung.core.analysis.sp_values import _values_match

if TYPE_CHECKING:
    from pyrung.core.analysis.pilot.effects import EffectExpectation
    from pyrung.core.analysis.pilot.options import CandidateRead
    from pyrung.core.analysis.pilot.overlay import PilotRung
    from pyrung.core.analysis.pilot.requirements import ActiveRequirement
    from pyrung.core.analysis.pilot.trace import TraceChoice
    from pyrung.core.analysis.pilot.working_theory import TheoryClaim, TheoryView


@dataclass(frozen=True)
class TargetSpec:
    """The target Compass is orienting toward."""

    tag: str
    value: Any
    predicate: Any = None


@dataclass(frozen=True)
class BearingObjective:
    """Target-relative meaning that must survive execution of one bearing.

    The navigation act owns the immediate physical boundary it will observe.
    This receipt owns why alternate landings may still be useful: the original
    user target plus the unresolved frontier Orientation read for that target.
    Verification and recovery carry it unchanged instead of reconstructing a
    weaker objective from the global context.
    """

    target: TargetSpec
    frontier: tuple[_ActionPair, ...] = ()

    def channel_goals(self, channel_tag: str) -> tuple[Any, ...]:
        """Ordered, de-duplicated target-relative goals for one channel."""
        goals: list[Any] = []
        for tag, value in self.frontier:
            if tag == channel_tag and not any(_values_match(value, goal) for goal in goals):
                goals.append(value)
        if self.target.tag == channel_tag and not any(
            _values_match(self.target.value, goal) for goal in goals
        ):
            goals.append(self.target.value)
        return tuple(goals)


@dataclass(frozen=True)
class NavigationConstraints:
    """User and world constraints applied before any path is selected."""

    blocked_actions: frozenset[_ActionPair] = frozenset()
    avoid_predicate: Any = None
    # Frozen Phase-4 schedule views. Orientation may use them for admissibility
    # but must never turn them into assignments or executable overlays here.
    active_requirements: tuple[ActiveRequirement, ...] = ()
    # Detached lifecycle evidence for this read. Compass may consult it but
    # cannot mutate the theory or retain an executable future through it.
    theory_view: TheoryView | None = None
    # Exact live requirements resolved by the drive for the active detached
    # temporal request. Empty on every ordinary orientation fast path.
    temporal_requirements: tuple[ActiveRequirement, ...] = ()
    # Exact (checkpoint owner, world key) selected as this read's executable
    # edge. Requirement evidence may come from several later diagnostics.
    temporal_source_anchor: tuple[Any, Any] | None = None


@dataclass(frozen=True)
class OrientationWorld:
    """Immutable handle to one current-world orientation frame.

    ``frame`` is the already-read trace frame.  ``state`` and ``context`` are
    private implementation handles used by internal readers; orientation must
    treat them as read-only.  Keeping the public world key and snapshot explicit
    makes stale-bearing checks independent of those implementation details.
    """

    world_key: _StateKey
    snapshot: dict[str, Any]
    frame: Any
    state: Any
    context: Any
    key_config: Any = None
    # Current-world root-route receipt selected by Orientation. This is one
    # read receipt used for execution/reporting, never a retained navigation
    # commitment or suffix of alternatives.
    root_route: TraceChoice | None = None


class ActSource(StrEnum):
    """The one provenance category Orientation assigned to an act."""

    ROUTE = "route"
    TRACE = "trace"
    LEARNED_ACTION = "learned_action"
    AWAITED_ACTION = "awaited_action"
    PROGRAM = "program"
    LEARNED_BATCH = "learned_batch"
    CROSSING = "crossing"
    WIDENING = "widening"
    TERMINAL = "terminal"


class ExpectationExemption(StrEnum):
    """Why an act deliberately makes no producer promise."""

    AMBIENT_TERMINAL = "ambient_terminal"
    UNRESOLVED_EFFECT = "unresolved_effect"


class LandingReceiptAuthority(StrEnum):
    """Which current-world reader owns supplemental landing interpretation."""

    ORIENTATION = "orientation"
    PROGRAM_STEP = "program_step"


class LocalProgressKind(StrEnum):
    """Physical lifecycle progress that need not move the global key."""

    TRACE_SETUP = "trace_setup"
    REARM = "rearm"
    TEMPORAL_SETUP = "temporal_setup"
    THEORY_CORRECTIVE = "theory_corrective"
    TEMPORAL_EDGE = "temporal_edge"
    OBSERVE_ENTRY = "observe_entry"


class PulseHorizon(StrEnum):
    """How far physical pulse execution may run before yielding to Compass."""

    ASSERTION_SCAN = "assertion_scan"
    LOOKAHEAD_SCAN = "lookahead_scan"


@dataclass(frozen=True)
class RouteEdgeContext:
    """The structural chart edge served by an immediate channel heading."""

    channel_tag: str
    from_value: Any
    target_value: Any
    # A pipeline route may advance a carrier by writing its actuation handoff.
    # Keep that exact writer effect visible so execution can receipt the
    # immediate handoff without assuming the carrier and selected effect are
    # the same tag.
    effect_tag: str | None = None
    effect_value: Any = None


@dataclass(frozen=True)
class ChannelHeading:
    """A channel boundary declared by navigation, before execution observes it."""

    channel_tag: str
    target_value: Any
    boundary: Any = None
    route: RouteEdgeContext | None = None


@dataclass(frozen=True)
class ActPolicy:
    """Navigation-owned meaning carried unchanged through one execution.

    ``heading`` is a declaration, not a claim about where execution landed.
    VERIFY combines it with the physical trial receipt when it owns that
    interpretation. Diagnostic provenance lives here too, so recording reads
    the same artifact that execution and verification consume.
    """

    source: ActSource
    action_pairs: tuple[_ActionPair, ...] = ()
    applied: tuple[_ActionPair, ...] = ()
    nogood_pair: _ActionPair | None = None
    heading: ChannelHeading | None = None
    motion: MotionKind = MotionKind.INTERVENTION
    provenance: tuple[str, ...] = ()
    downstream_reach: int | None = None
    note: str = ""
    context_actions: tuple[_ActionPair, ...] = ()
    expectation: EffectExpectation | None = None
    expectation_exemption: ExpectationExemption | None = None
    # Navigation provenance and landing-evidence ownership are orthogonal. A
    # route coast can still be owned by the ProgramStep which read its exact
    # present-tense producer and input handoffs.
    landing_receipt_authority: LandingReceiptAuthority = (
        LandingReceiptAuthority.ORIENTATION
    )
    local_progress: LocalProgressKind | None = None
    # Exact subset this local phase promises to establish. A SETUP_FIRST act
    # may install adjustable corrections while program-owned route facts stay
    # active for the next fresh Compass bearing.
    local_progress_requirements: tuple[ActiveRequirement, ...] = ()
    # Parent lifecycle obligations for the possibly branch-lowered VERIFY
    # requirements above. An accepted persistent setup phase discharges these
    # parents once their complete condition holds; the theory itself may stay
    # provisional while Compass follows program-owned work.
    local_progress_sources: tuple[ActiveRequirement, ...] = ()
    pulse_horizon: PulseHorizon = PulseHorizon.LOOKAHEAD_SCAN

    def __post_init__(self) -> None:
        if self.expectation is not None and self.expectation_exemption is not None:
            raise ValueError("an act cannot both promise and exempt an effect")

    @property
    def primary_action(self) -> _ActionPair | None:
        return self.action_pairs[0] if len(self.action_pairs) == 1 else None

    @property
    def observe_label(self) -> str:
        if self.source is ActSource.LEARNED_BATCH:
            return "batch"
        if self.source is ActSource.WIDENING:
            return "width"
        if self.motion is MotionKind.COAST_TO_BEARING:
            return "bearing_coast"
        if self.motion is MotionKind.COAST_HOLDING_WORLD:
            return "letrun"
        return "accept"

    @property
    def target_observe_label(self) -> str:
        return f"{self.observe_label}-target" if self.observe_label != "accept" else "target"

    @property
    def learned_prescribed(self) -> bool:
        return self.source in {ActSource.LEARNED_ACTION, ActSource.LEARNED_BATCH}

    @property
    def route_prescribed(self) -> bool:
        return self.source in {ActSource.ROUTE, ActSource.AWAITED_ACTION}

    @property
    def regression_nogoods(self) -> frozenset[_ActionPair]:
        # A joint overlay is one executable artifact. A rejection or later
        # regression must not poison either member as an independent scalar
        # action; singleton artifacts retain the established pair projection.
        return frozenset(self.applied) if len(self.applied) == 1 else frozenset()

    @property
    def chase_regression_causes(self) -> bool:
        return self.source not in {ActSource.LEARNED_BATCH, ActSource.WIDENING}


@dataclass(frozen=True)
class CrossingFidelity:
    """Reverse/proposal fidelity attached only to a grouped crossing act."""

    constraints: tuple[Any, ...]
    reason: str
    verify_required: bool
    exact: bool | None
    proposed: bool


@dataclass(frozen=True)
class Pulse:
    """One pulse act whose policy owns the exact action artifact."""

    policy: ActPolicy
    crossing: CrossingFidelity | None = None

    @property
    def action(self) -> _ActionPair:
        action = self.policy.primary_action
        if action is None:
            raise ValueError("a Pulse policy must declare exactly one primary action")
        return action

    @property
    def applied(self) -> tuple[_ActionPair, ...]:
        return self.policy.applied


@dataclass(frozen=True)
class BatchPulse:
    """One atomic joint pulse governed by the common act policy."""

    policy: ActPolicy
    crossing: CrossingFidelity | None = None

    @property
    def actions(self) -> tuple[_ActionPair, ...]:
        return self.policy.action_pairs


@dataclass(frozen=True)
class Coast:
    """One coast act consuming navigation's complete typed heading."""

    mode: Literal["bearing", "terminal"]
    policy: ActPolicy


@dataclass(frozen=True)
class Dwell:
    """One bounded verified dwell after a terminal coast receipt."""

    policy: ActPolicy = ActPolicy(
        ActSource.TERMINAL,
        motion=MotionKind.COAST_HOLDING_WORLD,
        expectation_exemption=ExpectationExemption.AMBIENT_TERMINAL,
    )


@dataclass(frozen=True)
class ObserveScan:
    """One program-owned scan used to establish an observed entry edge.

    This is a real navigation act, not an ambient settle.  It exists only at
    an unobserved invocation boundary, advances exactly one scan, and yields so
    Compass can bind the resulting projection to a fresh landing route.
    """

    policy: ActPolicy = ActPolicy(
        ActSource.PROGRAM,
        motion=MotionKind.COAST_HOLDING_WORLD,
        expectation_exemption=ExpectationExemption.UNRESOLVED_EFFECT,
        local_progress=LocalProgressKind.OBSERVE_ENTRY,
    )


NavigationAct = Pulse | BatchPulse | Coast | Dwell | ObserveScan


@dataclass(frozen=True)
class OrientationRead:
    """Named current-world readings and diagnostics for one orientation.

    ``world`` carries the fully assembled frame consumed by execution.
    ``candidates`` is the evidence-rich option reading that explains the
    selected result. These are named because they cross the orientation /
    orchestration boundary and have different contracts.
    """

    world_key: _StateKey
    world: OrientationWorld
    candidates: CandidateRead
    considered_paths: tuple[Any, ...] = ()
    rankings: tuple[Any, ...] = ()
    exclusions: tuple[Any, ...] = ()
    selected_bearing_id: str | None = None


@dataclass(frozen=True)
class Bearing:
    """Exactly one immediate executable act."""

    world_key: _StateKey
    act: NavigationAct
    objective: BearingObjective
    prerequisites: tuple[PilotRung, ...] = ()
    rationale: str = ""
    orientation: OrientationRead | None = None
    # Ambient maintenance may be claim-free. Orientation owns enforcing that
    # every selected causal Bearing carries this detached producer claim.
    claim: TheoryClaim | None = None
    # Detached chart artifact selected as this exact first move. It is scoped
    # by the attached theory/version/source when recorded, never replayed.
    first_edge_identity: tuple[Any, ...] | None = None

    @property
    def expectation(self) -> EffectExpectation | None:
        """The act-owned obligation, exposed without rebuilding policy."""

        return self.act.policy.expectation


@dataclass(frozen=True)
class ProbeRequest:
    """Declarative request for an isolated frontier probe."""

    frontier: tuple[_ActionPair, ...]
    reason: str


@dataclass(frozen=True)
class NeedProbe:
    """Compass cannot orient until an isolated probe adds evidence."""

    world_key: _StateKey
    frontier: tuple[_ActionPair, ...]
    request: ProbeRequest
    rationale: str
    provenance: tuple[str, ...] = ()
    orientation: OrientationRead | None = None


@dataclass(frozen=True)
class Stuck:
    """Structured, evidence-backed terminal navigation diagnosis."""

    world_key: _StateKey
    reason_code: str
    frontier: tuple[_ActionPair, ...] = ()
    exclusions: tuple[Any, ...] = ()
    evidence: tuple[Any, ...] = ()
    rationale: str = ""
    orientation: OrientationRead | None = None


OrientationResult = Bearing | NeedProbe | Stuck


def _applied_identity(applied: tuple[_ActionPair, ...]) -> tuple[_ActionPair, ...]:
    """Canonical identity of one atomic applied overlay."""
    return tuple(
        sorted(
            ((tag, _semantic_key(value)) for tag, value in applied),
            key=lambda pair: (pair[0], repr(pair[1])),
        )
    )


def pulse_identity(applied: tuple[_ActionPair, ...]) -> tuple[Any, ...]:
    """Exact executable identity of one Pulse action overlay."""

    return ("pulse", _applied_identity(applied))


def act_identity(act: NavigationAct) -> tuple[Any, ...]:
    """Canonical executable identity used for empirical receipts."""

    if isinstance(act, Pulse):
        return pulse_identity(act.applied)
    if isinstance(act, BatchPulse):
        return pulse_identity(act.policy.applied)
    if isinstance(act, Coast):
        heading = act.policy.heading
        route = heading.route if heading is not None else None
        identity = (
            "coast",
            act.mode,
            _applied_identity(act.policy.applied),
            heading.channel_tag if heading is not None else None,
            _semantic_key(heading.target_value if heading is not None else None),
            _semantic_key(heading.boundary if heading is not None else None),
        )
        if route is not None:
            return (
                *identity,
                route.channel_tag,
                _semantic_key(route.target_value),
                route.effect_tag,
                _semantic_key(route.effect_value),
            )
        return identity
    if isinstance(act, ObserveScan):
        return ("observe-scan",)
    return ("dwell", _applied_identity(act.policy.applied))
