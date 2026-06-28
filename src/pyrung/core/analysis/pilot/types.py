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
    from pyrung.core.analysis.pilot.compass import Compass
    from pyrung.core.analysis.pilot.evidence import PipelineRoles, TransitionEvidence
    from pyrung.core.analysis.pilot.outcome import Outcome
    from pyrung.core.analysis.pilot.trace import TraceAction, TraceChoice
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
    action: dict[str, Any]
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


# ---------------------------------------------------------------------------
# Context / state / frame
# ---------------------------------------------------------------------------


@dataclass
class _PilotContext:
    target_tag: str
    target_value: Any
    pdg: ProgramGraph
    program: Any
    steerable: frozenset[str]
    edge_tags: set[str]
    resting: dict[str, Any]
    nd_domains: dict[str, tuple[Any, ...]] | None
    evidence: TransitionEvidence | None
    compass: Compass
    opaque_loop: frozenset[str]
    pipeline_roles: tuple[PipelineRoles, ...]
    pipeline_internal_tags: frozenset[str]
    choice: TraceChoice | None
    blocked_choice_actions: frozenset[_ActionPair]
    max_scans: int
    live: bool
    debug: bool
    avoid_pred: Any = None

    def route_allowed(self, pair: _ActionPair) -> bool:
        return pair not in self.blocked_choice_actions


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
    action: dict[str, Any]
    pulse_actions: tuple[_ActionPair, ...]
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
