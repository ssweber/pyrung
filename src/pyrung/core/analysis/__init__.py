"""Static analysis helpers for pyrung programs."""

from pyrung.core.analysis.affine import (
    AffineForm,
    extract_affine_expression,
    extract_forward_affine,
    extract_forward_offset,
)
from pyrung.core.analysis.dataview import DataView, TagNameMatcher
from pyrung.core.analysis.graph import (
    Plan,
    PlanStatus,
    RouteAlt,
    RoutePivot,
    RouteTaken,
)
from pyrung.core.analysis.pdg import (
    CallSite,
    ProgramGraph,
    RungNode,
    TagRole,
    TagVersion,
    build_program_graph,
    classify_tags,
    collect_program_tags,
)
from pyrung.core.analysis.prove import (
    Counterexample,
    Decision,
    Intractable,
    Journal,
    Proven,
    StateDiff,
    TagEntry,
    TraceStep,
    always,
    diff_states,
    never,
    reachable_states,
)
from pyrung.core.analysis.return_guards import (
    ReachChain,
    effective_reach_chains,
    return_early_guard_exprs,
    scope_reach_chains,
)
from pyrung.core.analysis.simplified import TerminalForm, simplified_forms
from pyrung.core.analysis.value_domains import (
    closed_value_domains,
    declared_value_domain,
    produced_value_domains,
)

__all__ = [
    "AffineForm",
    "CallSite",
    "Counterexample",
    "DataView",
    "Decision",
    "Journal",
    "Intractable",
    "Plan",
    "PlanStatus",
    "ProgramGraph",
    "Proven",
    "RouteAlt",
    "RoutePivot",
    "RouteTaken",
    "ReachChain",
    "RungNode",
    "StateDiff",
    "TagEntry",
    "TagNameMatcher",
    "TagRole",
    "TagVersion",
    "TerminalForm",
    "TraceStep",
    "build_program_graph",
    "classify_tags",
    "closed_value_domains",
    "collect_program_tags",
    "declared_value_domain",
    "diff_states",
    "effective_reach_chains",
    "extract_affine_expression",
    "extract_forward_affine",
    "extract_forward_offset",
    "always",
    "never",
    "produced_value_domains",
    "reachable_states",
    "return_early_guard_exprs",
    "scope_reach_chains",
    "simplified_forms",
]
