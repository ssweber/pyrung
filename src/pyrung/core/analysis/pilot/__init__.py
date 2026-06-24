"""PILOT — Probe, Input, Let-run, Observe, Trace.

Backward-trace + forward-simulate engine for reaching target states.
Replaces walk/ with 2 files instead of 12.
"""

from __future__ import annotations

from pyrung.core.analysis.pilot.evidence import (
    TransitionEvidence,
    TransitionRoute,
    expand_routes,
    seed_influence_from_routes,
)
from pyrung.core.analysis.pilot.pilot import (
    PilotEvent,
    PilotGateEvent,
    TagChange,
    pilot_drive,
    pilot_events,
    pilot_how,
)
from pyrung.core.analysis.pilot.trace import TraceAction, TraceChoice, TraceNode, trace_back

__all__ = [
    "PilotEvent",
    "PilotGateEvent",
    "TagChange",
    "TraceChoice",
    "TraceAction",
    "TraceNode",
    "TransitionEvidence",
    "TransitionRoute",
    "expand_routes",
    "pilot_drive",
    "pilot_events",
    "pilot_how",
    "seed_influence_from_routes",
    "trace_back",
]
