"""Shared immutable navigation contracts for PILOT.

The types in this module are deliberately free of reading and execution
algorithms.  ``Compass`` produces these values, ``pilot.py`` dispatches them,
and ``steer.py`` / ``skiff.py`` execute the declared work.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Literal

from pyrung.core.analysis.pilot.types import _ActionPair, _StateKey
from pyrung.core.analysis.sp_values import _values_match

if TYPE_CHECKING:
    from pyrung.core.analysis.pilot.trace import TraceChoice


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


@dataclass(frozen=True)
class Pulse:
    """One pulse act, including any atomic co-actions."""

    action: _ActionPair
    applied: tuple[_ActionPair, ...]
    option: Any


@dataclass(frozen=True)
class BatchPulse:
    """One atomic joint pulse."""

    actions: tuple[_ActionPair, ...]
    source: Literal["learned", "widening"]


@dataclass(frozen=True)
class Coast:
    """One coast act with an executable boundary and optional route heading.

    ``channel_tag`` / ``target_value`` are the immediate value the executor
    must witness. ``route_*`` names the outer chart edge that the local proof
    serves; it is presentation and nogood context, never permission to coast
    past the witnessed boundary.
    """

    mode: Literal["bearing", "terminal"]
    channel_tag: str | None = None
    target_value: Any = None
    # The owner's original relation. ``target_value`` is its exact observable
    # heading; execution keeps this proof for progress and coast estimates.
    boundary: Any = None
    route_prescribed: bool = False
    route_channel_tag: str | None = None
    route_from_value: Any = None
    route_target_value: Any = None


@dataclass(frozen=True)
class Dwell:
    """One bounded verified dwell after a terminal coast receipt."""


NavigationAct = Pulse | BatchPulse | Coast | Dwell


@dataclass(frozen=True)
class OrientationTrace:
    """Named current-world readings and diagnostics for one orientation.

    ``world`` carries the fully assembled frame consumed by execution.
    ``candidates`` is the evidence-rich option reading that explains the
    selected result. These are named because they cross the orientation /
    orchestration boundary and have different contracts.
    """

    world_key: _StateKey
    world: OrientationWorld
    candidates: Any
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
    prerequisites: tuple[Any, ...] = ()
    rationale: str = ""
    trace: OrientationTrace | None = None


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
    trace: OrientationTrace | None = None


@dataclass(frozen=True)
class Stuck:
    """Structured, evidence-backed terminal navigation diagnosis."""

    world_key: _StateKey
    reason_code: str
    frontier: tuple[_ActionPair, ...] = ()
    exclusions: tuple[Any, ...] = ()
    evidence: tuple[Any, ...] = ()
    rationale: str = ""
    trace: OrientationTrace | None = None


OrientationResult = Bearing | NeedProbe | Stuck


def pulse_identity(applied: tuple[_ActionPair, ...]) -> tuple[Any, ...]:
    """Exact executable identity of one Pulse action overlay."""

    return ("pulse", applied)


def act_identity(act: NavigationAct) -> tuple[Any, ...]:
    """Stable identity used for world-scoped empirical nogoods."""

    if isinstance(act, Pulse):
        return pulse_identity(act.applied)
    if isinstance(act, BatchPulse):
        return ("batch", act.source, act.actions)
    if isinstance(act, Coast):
        identity = (
            "coast",
            act.mode,
            act.channel_tag,
            repr(act.target_value),
            repr(act.boundary),
        )
        if act.route_channel_tag is not None:
            return (
                *identity,
                act.route_channel_tag,
                repr(act.route_from_value),
                repr(act.route_target_value),
            )
        return identity
    return ("dwell",)
