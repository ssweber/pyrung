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
from pyrung.core.analysis.return_guards import (
    ReachChain,
    effective_reach_chains,
    return_early_guard_exprs,
    scope_reach_chains,
)
from pyrung.core.analysis.simplified import PermissiveSet, TerminalForm, simplified_forms
from pyrung.core.analysis.value_domains import (
    closed_value_domains,
    declared_value_domain,
    produced_value_domains,
)

__all__ = [
    "AffineForm",
    "CallSite",
    "DataView",
    "Plan",
    "PlanStatus",
    "PermissiveSet",
    "ProgramGraph",
    "RouteAlt",
    "RoutePivot",
    "RouteTaken",
    "ReachChain",
    "RungNode",
    "TagNameMatcher",
    "TagRole",
    "TagVersion",
    "TerminalForm",
    "build_program_graph",
    "classify_tags",
    "closed_value_domains",
    "collect_program_tags",
    "declared_value_domain",
    "effective_reach_chains",
    "extract_affine_expression",
    "extract_forward_affine",
    "extract_forward_offset",
    "produced_value_domains",
    "return_early_guard_exprs",
    "scope_reach_chains",
    "simplified_forms",
]
