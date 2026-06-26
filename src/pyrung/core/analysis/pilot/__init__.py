"""PILOT — drive a PLC program from its current state to a target.

Backward-trace + forward-simulate engine.  Navigation is organised around the
*compass* (``compass.py``): ``trace`` (static reader) + ``let-run`` + ``sandbox``.
See ``pilot/CLAUDE.md``.
"""

from __future__ import annotations

from pyrung.core.analysis.pilot.compass import (
    Compass,
    CompassEdge,
    CompassGraph,
    CompassPlan,
    best_compass_plan,
    build_compass_graphs,
    detect_opaque_loop,
    detect_opaque_pipelines,
)
from pyrung.core.analysis.pilot.evidence import (
    PipelineRoles,
    TransitionEvidence,
    TransitionRoute,
    expand_routes,
    infer_pipeline_roles,
)
from pyrung.core.analysis.pilot.investigate import (
    BearingDeparture,
    DeviationIncident,
    InvestigationHypothesis,
    InvestigationResult,
    ReplayOutcome,
    build_deviation_incident,
    investigate_deviation,
)
from pyrung.core.analysis.pilot.pilot import (
    PilotEvent,
    PilotGateEvent,
    TagChange,
    pilot_drive,
    pilot_events,
    pilot_how,
)
from pyrung.core.analysis.pilot.sandbox import (
    PipelineNeedExpansion,
    SandboxResult,
    expand_pipeline_need,
    participating_tags_for_sandbox,
    roles_for_needed_tag,
    run_sandbox_scan,
)
from pyrung.core.analysis.pilot.trace import TraceAction, TraceChoice, TraceNode, trace_back

__all__ = [
    "PilotEvent",
    "PilotGateEvent",
    "TagChange",
    "TraceChoice",
    "TraceAction",
    "TraceNode",
    "Compass",
    "CompassEdge",
    "CompassGraph",
    "CompassPlan",
    "PipelineRoles",
    "SandboxResult",
    "PipelineNeedExpansion",
    "BearingDeparture",
    "DeviationIncident",
    "InvestigationHypothesis",
    "InvestigationResult",
    "ReplayOutcome",
    "TransitionEvidence",
    "TransitionRoute",
    "best_compass_plan",
    "build_deviation_incident",
    "build_compass_graphs",
    "detect_opaque_loop",
    "detect_opaque_pipelines",
    "expand_routes",
    "expand_pipeline_need",
    "participating_tags_for_sandbox",
    "infer_pipeline_roles",
    "investigate_deviation",
    "pilot_drive",
    "pilot_events",
    "pilot_how",
    "roles_for_needed_tag",
    "run_sandbox_scan",
    "trace_back",
]
