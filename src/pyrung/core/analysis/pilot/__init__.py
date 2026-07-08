"""PILOT — drive a PLC program from its current state to a target.

Backward-trace + forward-simulate engine.  Navigation is organised around the
*compass* (``compass.py``): ``trace`` (static reader) + ``let-run`` + ``skiff``.
See ``pilot/CLAUDE.md``.
"""

from __future__ import annotations

from pyrung.core.analysis.pilot.causal import chase_cause_roots
from pyrung.core.analysis.pilot.charts import (
    CompassEdge,
    CompassGraph,
    CompassPlan,
    best_compass_plan,
    build_compass_graphs,
    detect_opaque_loop,
    detect_opaque_pipelines,
)
from pyrung.core.analysis.pilot.compass import (
    Compass,
)
from pyrung.core.analysis.pilot.evidence import (
    PipelineNeedExpansion,
    PipelineRoles,
    TransitionEvidence,
    TransitionRoute,
    expand_pipeline_need,
    expand_routes,
    infer_pipeline_roles,
    roles_for_needed_tag,
)
from pyrung.core.analysis.pilot.investigate import (
    BearingDeparture,
    DeviationIncident,
    ExcursionResult,
    InvestigationHypothesis,
    InvestigationResult,
    ReplayOutcome,
    build_deviation_incident,
    build_replay_fn,
    investigate_deviation,
    investigate_excursion,
)
from pyrung.core.analysis.pilot.pilot import (
    pilot_drive,
    pilot_events,
    pilot_how,
)
from pyrung.core.analysis.pilot.skiff import (
    SkiffResult,
    participating_tags_for_skiff,
    run_skiff_scan,
)
from pyrung.core.analysis.pilot.trace import TraceAction, TraceChoice, TraceNode, trace_back
from pyrung.core.analysis.pilot.types import PilotEvent, PilotGateEvent, TagChange

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
    "SkiffResult",
    "PipelineNeedExpansion",
    "BearingDeparture",
    "DeviationIncident",
    "ExcursionResult",
    "InvestigationHypothesis",
    "InvestigationResult",
    "ReplayOutcome",
    "TransitionEvidence",
    "TransitionRoute",
    "best_compass_plan",
    "build_deviation_incident",
    "build_replay_fn",
    "build_compass_graphs",
    "chase_cause_roots",
    "detect_opaque_loop",
    "detect_opaque_pipelines",
    "expand_routes",
    "expand_pipeline_need",
    "participating_tags_for_skiff",
    "infer_pipeline_roles",
    "investigate_deviation",
    "investigate_excursion",
    "pilot_drive",
    "pilot_events",
    "pilot_how",
    "roles_for_needed_tag",
    "run_skiff_scan",
    "trace_back",
]
