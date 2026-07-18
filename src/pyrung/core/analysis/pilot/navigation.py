"""Shared immutable navigation contracts for PILOT.

The types in this module are deliberately free of reading and execution
algorithms.  ``Compass`` produces these values, ``pilot.py`` dispatches them,
and ``steer.py`` / ``skiff.py`` execute the declared work.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Generic, Literal, TypeVar

from pyrung.core.analysis.pilot.types import _ActionPair, _StateKey

T = TypeVar("T")


@dataclass(frozen=True)
class Known(Generic[T]):
    """A reader established a value."""

    value: T


@dataclass(frozen=True)
class Unknown:
    """A reader could not resolve a frontier soundly."""

    reason: str
    frontier: tuple[_ActionPair, ...] = ()


@dataclass(frozen=True)
class Impossible:
    """A complete proof established that an option cannot work."""

    proof: str


EvidenceResult = Known[T] | Unknown | Impossible


@dataclass(frozen=True)
class TargetSpec:
    """The target Compass is orienting toward."""

    tag: str
    value: Any
    predicate: Any = None


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
    """One coast act, carrying only its immediate execution heading."""

    mode: Literal["bearing", "terminal"]
    channel_tag: str | None = None
    target_value: Any = None
    route_prescribed: bool = False


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
    prerequisites: tuple[Any, ...] = ()
    immediate_goal: Any = None
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


def act_identity(act: NavigationAct) -> tuple[Any, ...]:
    """Stable identity used for world-scoped empirical nogoods."""

    if isinstance(act, Pulse):
        return ("pulse", act.applied)
    if isinstance(act, BatchPulse):
        return ("batch", act.source, act.actions)
    if isinstance(act, Coast):
        return ("coast", act.mode, act.channel_tag, repr(act.target_value))
    return ("dwell",)
