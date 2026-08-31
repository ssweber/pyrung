"""Subroutine reachability and recursion validation."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from pyrung.core.validation._common import compact_location
from pyrung.core.validation.display import FindingDisplay, Frame, _FindingTextMixin
from pyrung.core.validation.severity import Severity

if TYPE_CHECKING:
    from pyrung.core.analysis.pdg import CallSite, ProgramGraph
    from pyrung.core.program import Program


CALL_NEVER_CALLED = "CALL_NEVER_CALLED"
CALL_RECURSION = "CALL_RECURSION"


@dataclass(frozen=True)
class CallGraphFinding(_FindingTextMixin):
    code: str
    target_name: str
    display: FindingDisplay
    severity: Severity
    cycle: tuple[str, ...] = ()

    @property
    def message(self) -> str:
        return self.display.as_text()


@dataclass(frozen=True)
class CallGraphReport:
    findings: tuple[CallGraphFinding, ...]

    def summary(self) -> str:
        if not self.findings:
            return "No call-graph findings."
        return f"{len(self.findings)} call-graph finding(s)."


def _reachable_from_main(sites: tuple[CallSite, ...]) -> frozenset[str]:
    edges: dict[str | None, set[str]] = {}
    for site in sites:
        edges.setdefault(site.caller, set()).add(site.callee)
    reached: set[str] = set()
    pending = list(edges.get(None, ()))
    while pending:
        name = pending.pop()
        if name in reached:
            continue
        reached.add(name)
        pending.extend(edges.get(name, ()))
    return frozenset(reached)


def _recursive_components(
    names: tuple[str, ...], sites: tuple[CallSite, ...]
) -> tuple[tuple[str, ...], ...]:
    """Return recursive strongly connected components in deterministic order."""
    graph: dict[str, tuple[str, ...]] = {name: () for name in names}
    mutable: dict[str, set[str]] = {name: set() for name in names}
    for site in sites:
        if site.caller in mutable and site.callee in mutable:
            mutable[site.caller].add(site.callee)
    graph = {name: tuple(sorted(targets)) for name, targets in mutable.items()}

    index = 0
    indexes: dict[str, int] = {}
    low: dict[str, int] = {}
    stack: list[str] = []
    on_stack: set[str] = set()
    components: list[tuple[str, ...]] = []

    def visit(name: str) -> None:
        nonlocal index
        indexes[name] = index
        low[name] = index
        index += 1
        stack.append(name)
        on_stack.add(name)
        for target in graph[name]:
            if target not in indexes:
                visit(target)
                low[name] = min(low[name], low[target])
            elif target in on_stack:
                low[name] = min(low[name], indexes[target])
        if low[name] != indexes[name]:
            return
        component: list[str] = []
        while stack:
            member = stack.pop()
            on_stack.remove(member)
            component.append(member)
            if member == name:
                break
        ordered = tuple(sorted(component))
        if len(ordered) > 1 or (ordered and ordered[0] in graph[ordered[0]]):
            components.append(ordered)

    for name in names:
        if name not in indexes:
            visit(name)
    return tuple(sorted(components))


def _cycle_path(component: tuple[str, ...], sites: tuple[CallSite, ...]) -> tuple[str, ...]:
    members = set(component)
    edges: dict[str, list[str]] = {name: [] for name in component}
    for site in sites:
        if site.caller in members and site.callee in members:
            edges[site.caller].append(site.callee)
    for targets in edges.values():
        targets.sort()

    start = component[0]

    def find(current: str, path: tuple[str, ...]) -> tuple[str, ...] | None:
        for target in edges[current]:
            if target == start:
                return (*path, start)
            if target not in path:
                found = find(target, (*path, target))
                if found is not None:
                    return found
        return None

    return find(start, (start,)) or (*component, component[0])


def _site_location(graph: ProgramGraph, site: CallSite) -> str:
    node = graph.rung_nodes[site.node_index]
    return compact_location(node.scope, node.subroutine, node.rung_index, node.branch_path)


def validate_call_graph(program: Program) -> CallGraphReport:
    """Report subroutines unreachable from Main and recursive call components."""
    from pyrung.core.analysis.pdg import build_program_graph

    graph = build_program_graph(program)
    sites = graph.call_sites()
    names = tuple(sorted(program.subroutines))
    reached = _reachable_from_main(sites)
    findings: list[CallGraphFinding] = []

    for name in names:
        if name in reached:
            continue
        findings.append(
            CallGraphFinding(
                code=CALL_NEVER_CALLED,
                target_name=name,
                display=FindingDisplay(
                    code=CALL_NEVER_CALLED,
                    severity="info",
                    frames=(Frame(location=name),),
                    problem=f"Subroutine {name} has no call path from Main.",
                    hint=f'call("{name}") from Main or remove the unused subroutine',
                ),
                severity="info",
            )
        )

    for component in _recursive_components(names, sites):
        cycle = _cycle_path(component, sites)
        cycle_edges = set(zip(cycle[:-1], cycle[1:], strict=True))
        frames = tuple(
            Frame(
                location=_site_location(graph, site),
                lines=(f'call("{site.callee}")',),
                caret=(0, 6, len(site.callee) + 2),
                caret_label="recursive call",
            )
            for site in sites
            if site.caller is not None and (site.caller, site.callee) in cycle_edges
        )
        route = " -> ".join(cycle)
        findings.append(
            CallGraphFinding(
                code=CALL_RECURSION,
                target_name=component[0],
                display=FindingDisplay(
                    code=CALL_RECURSION,
                    severity="error",
                    frames=frames,
                    problem=f"Recursive subroutine cycle: {route}.",
                    hint="break the cycle; subroutine calls must form an acyclic graph",
                ),
                severity="error",
                cycle=cycle,
            )
        )

    return CallGraphReport(findings=tuple(findings))


__all__ = [
    "CALL_NEVER_CALLED",
    "CALL_RECURSION",
    "CallGraphFinding",
    "CallGraphReport",
    "validate_call_graph",
]
