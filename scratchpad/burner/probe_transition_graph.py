"""Prototype a local transition-graph learner for an opaque register.

The learner forks from observed states, runs full-scan trials, derives a
participation boundary from causal evidence, records only local graph edges,
and discards each trial fork.  It does not commit anything to PILOT.
"""

from __future__ import annotations

import argparse
import os
import sys
from collections import deque
from dataclasses import dataclass
from pathlib import Path
from typing import Any

CLICK_PROJECT = Path(
    os.environ.get(
        "PYRUNG_CLICK_PROJECT",
        r"C:\Users\Sam\AppData\Local\Temp\CLICK (0009051C)\pyrung_project",
    )
)
sys.path.insert(0, str(CLICK_PROJECT))

from main import logic  # noqa: E402

from pyrung import PLC  # noqa: E402
from pyrung.core.analysis.causal.models import CausalChain, ChainStep  # noqa: E402
from pyrung.core.analysis.pdg import ProgramGraph, build_program_graph  # noqa: E402

Action = tuple[str, Any]

PHYSICAL_PERMISSIVES = {
    "x_DoorClosed": True,
    "x_LintDoorClosed": True,
    "x_BlowerFB": True,
    "x_RotateFB": True,
    "x_RotateSensor": False,
    "x_SailRelay": True,
}

DEFAULT_ACTIONS: tuple[Action, ...] = (
    ("C_Clear", True),
    ("C_Reset", True),
    ("C_Start", True),
    ("C_Stop", True),
    ("C_Abort", True),
    ("C_ForceClear", True),
    ("C_ResetToFactoryDefaults", True),
)


@dataclass(frozen=True)
class Boundary:
    tags: frozenset[str]
    node_indices: frozenset[int]
    timeline_rungs: frozenset[int]


@dataclass(frozen=True)
class EdgeObservation:
    from_value: Any
    to_value: Any
    cause: str
    scans: int
    scan_id: int
    prerequisites: tuple[tuple[str, Any], ...]
    participating_delta: tuple[tuple[str, Any, Any], ...]
    external_delta_count: int
    external_delta_sample: tuple[tuple[str, Any, Any], ...]
    boundary: Boundary
    chain: CausalChain | None


def _parse_value(text: str) -> Any:
    lowered = text.lower()
    if lowered == "true":
        return True
    if lowered == "false":
        return False
    if lowered in {"none", "null"}:
        return None
    try:
        return int(text)
    except ValueError:
        pass
    try:
        return float(text)
    except ValueError:
        return text


def _parse_action(text: str) -> Action:
    if "=" not in text:
        raise argparse.ArgumentTypeError("actions must be TAG=VALUE")
    tag, value = text.split("=", 1)
    tag = tag.strip()
    if not tag:
        raise argparse.ArgumentTypeError("action tag cannot be empty")
    return (tag, _parse_value(value.strip()))


def _snapshot(plc: PLC) -> dict[str, Any]:
    return dict(plc.state.tags)


def _changed(before: dict[str, Any], after: dict[str, Any]) -> list[tuple[str, Any, Any]]:
    tags = set(before) | set(after)
    return sorted((tag, before.get(tag), after.get(tag)) for tag in tags if before.get(tag) != after.get(tag))


def _known(plc: PLC, tag: str) -> bool:
    known = getattr(plc, "_known_tags_by_name", {})
    return tag in known or tag in plc.state.tags


def _format_action(action: Action | None) -> str:
    if action is None:
        return "SCAN"
    tag, value = action
    return f"{tag}={value!r}"


def _step_key(step: ChainStep) -> tuple[str | None, int]:
    return (step.subroutine, step.rung_index)


def _node_indices_for_step(pdg: ProgramGraph, step: ChainStep) -> set[int]:
    key = _step_key(step)
    indices = {
        idx
        for idx, node in enumerate(pdg.rung_nodes)
        if node.subroutine == key[0] and node.rung_index == key[1]
    }
    if step.caller_rung_index is not None:
        indices.update(
            idx
            for idx, node in enumerate(pdg.rung_nodes)
            if node.subroutine is None
            and node.rung_index == step.caller_rung_index
            and not node.branch_path
        )
    return indices


def _derive_boundary(
    *,
    target: str,
    chain: CausalChain | None,
    pdg: ProgramGraph,
) -> Boundary:
    tags: set[str] = {target}
    node_indices: set[int] = set()

    if chain is not None:
        tags.add(chain.effect.tag_name)
        for effect in chain.effects:
            tags.add(effect.tag_name)
        for root in chain.conjunctive_roots:
            tags.add(root.tag_name)
        for root in chain.ambiguous_roots:
            tags.add(root.tag_name)
        for step in chain.steps:
            tags.add(step.transition.tag_name)
            tags.update(trigger.tag_name for trigger in step.triggers)
            tags.update(enabler.tag_name for enabler in step.enablers)
            node_indices.update(_node_indices_for_step(pdg, step))

    for node_index in tuple(node_indices):
        node = pdg.rung_nodes[node_index]
        tags.update(node.condition_reads)
        tags.update(node.data_reads)
        tags.update(node.writes)
        tags.update(node.implicit_writes)
        tags.update(node.guard_reads)
        for call in node.calls:
            for member_idx in pdg._subroutine_member_indices().get(call, ()):
                member = pdg.rung_nodes[member_idx]
                tags.update(member.condition_reads)
                tags.update(member.data_reads)
                tags.update(member.writes)
                node_indices.add(member_idx)

    timeline_rungs: set[int] = set()
    for tag in tags:
        timeline_rungs.update(pdg.timeline_writers_of(tag))

    return Boundary(
        tags=frozenset(tags),
        node_indices=frozenset(node_indices),
        timeline_rungs=frozenset(timeline_rungs),
    )


def _extract_prerequisites(chain: CausalChain | None, target: str) -> tuple[tuple[str, Any], ...]:
    if chain is None:
        return ()
    prereqs: dict[str, Any] = {}
    for step in chain.steps:
        for enabler in step.enablers:
            if enabler.tag_name != target:
                prereqs.setdefault(enabler.tag_name, enabler.value)
    return tuple(sorted(prereqs.items()))


def _observe_edge(
    *,
    target: str,
    before: dict[str, Any],
    after_plc: PLC,
    cause: str,
    scans: int,
    pdg: ProgramGraph,
) -> EdgeObservation | None:
    after = _snapshot(after_plc)
    from_value = before.get(target)
    to_value = after.get(target)
    if from_value == to_value:
        return None

    chain = after_plc.cause(target, scan=after_plc.state.scan_id)
    boundary = _derive_boundary(target=target, chain=chain, pdg=pdg)
    all_delta = _changed(before, after)
    participating = tuple(delta for delta in all_delta if delta[0] in boundary.tags)
    external = tuple(delta for delta in all_delta if delta[0] not in boundary.tags)
    return EdgeObservation(
        from_value=from_value,
        to_value=to_value,
        cause=cause,
        scans=scans,
        scan_id=after_plc.state.scan_id,
        prerequisites=_extract_prerequisites(chain, target),
        participating_delta=participating,
        external_delta_count=len(external),
        external_delta_sample=external[:12],
        boundary=boundary,
        chain=chain,
    )


def _run_trial(
    *,
    base: PLC,
    target: str,
    action: Action | None,
    max_scans: int,
    pdg: ProgramGraph,
    animate_rotate_sensor: bool,
) -> tuple[EdgeObservation | None, PLC]:
    trial = base.fork()
    before = _snapshot(trial)
    if action is not None:
        tag, value = action
        trial.patch({tag: value})

    for scan_count in range(1, max_scans + 1):
        if animate_rotate_sensor and _known(trial, "x_RotateSensor"):
            trial.force("x_RotateSensor", (trial.state.scan_id // 50) % 2 == 0)
        trial.step()
        edge = _observe_edge(
            target=target,
            before=before,
            after_plc=trial,
            cause=_format_action(action),
            scans=scan_count,
            pdg=pdg,
        )
        if edge is not None:
            return edge, trial
    return None, trial


def _print_edge(edge: EdgeObservation) -> None:
    print(
        f"\nEDGE {edge.from_value!r} -> {edge.to_value!r} "
        f"via {edge.cause} scans={edge.scans} at scan={edge.scan_id}"
    )
    if edge.prerequisites:
        print("  prerequisites:")
        for tag, value in edge.prerequisites:
            print(f"    - {tag}={value!r}")
    else:
        print("  prerequisites: -")
    print(
        "  boundary: "
        f"tags={len(edge.boundary.tags)} nodes={len(edge.boundary.node_indices)} "
        f"timeline_rungs={sorted(r + 1 for r in edge.boundary.timeline_rungs)[:16]}"
    )
    print("  participating_delta:")
    for tag, old, new in edge.participating_delta[:24]:
        print(f"    - {tag}: {old!r} -> {new!r}")
    if len(edge.participating_delta) > 24:
        print(f"    ... {len(edge.participating_delta) - 24} more")
    print(f"  external_delta_count={edge.external_delta_count}")
    if edge.external_delta_sample:
        print("  external_delta_sample:")
        for tag, old, new in edge.external_delta_sample:
            print(f"    - {tag}: {old!r} -> {new!r}")
    if edge.chain is not None:
        print("  evidence:")
        for index, step in enumerate(edge.chain.steps[:8]):
            triggers = ", ".join(f"{t.tag_name}:{t.from_value!r}->{t.to_value!r}" for t in step.triggers)
            enablers = ", ".join(f"{e.tag_name}={e.value!r}" for e in step.enablers)
            location = f"{step.subroutine or 'main'} r{step.rung_index + 1}"
            caller = f" caller=r{step.caller_rung_index + 1}" if step.caller_rung_index is not None else ""
            print(
                f"    [{index}] {location}{caller} "
                f"{step.transition.tag_name}:{step.transition.from_value!r}->{step.transition.to_value!r}"
            )
            print(f"        triggers={triggers or '-'}")
            print(f"        enablers={enablers or '-'}")


def _print_sibling_groups(edge: EdgeObservation, pdg: ProgramGraph) -> None:
    if edge.chain is None:
        return
    printed: set[tuple[str | None, str]] = set()
    print("  sibling writer groups:")
    for step in edge.chain.steps:
        key = (step.subroutine, step.transition.tag_name)
        if key in printed:
            continue
        printed.add(key)
        siblings = [
            (idx, node)
            for idx, node in enumerate(pdg.rung_nodes)
            if node.subroutine == step.subroutine and step.transition.tag_name in node.writes
        ]
        if len(siblings) <= 1:
            continue
        print(f"    {step.subroutine or 'main'} writes {step.transition.tag_name}:")
        for idx, node in siblings[:16]:
            print(
                f"      node={idx} rung={node.rung_index + 1} "
                f"conds={sorted(node.condition_reads)} data={sorted(node.data_reads)}"
            )
        if len(siblings) > 16:
            print(f"      ... {len(siblings) - 16} more")


def _setup_burner(plc: PLC) -> None:
    for name, value in PHYSICAL_PERMISSIVES.items():
        if _known(plc, name):
            plc.force(name, value)
    plc.step()
    plc.patch({"C_ProductionMode": True, "C_UnitModeChgRequest": True})
    plc.step()
    for _ in range(3):
        plc.step()


def _learn_graph(
    *,
    root: PLC,
    target: str,
    actions: tuple[Action, ...],
    max_depth: int,
    max_scans: int,
    pdg: ProgramGraph,
    animate_rotate_sensor: bool,
    stop_after_first_edge: bool,
) -> list[EdgeObservation]:
    graph: dict[tuple[Any, str], EdgeObservation] = {}
    queued_values: set[Any] = {root.state.tags.get(target)}
    frontier: deque[tuple[int, PLC, str]] = deque([(0, root, "root")])

    while frontier:
        depth, base, label = frontier.popleft()
        current = base.state.tags.get(target)
        print(f"\n=== Frontier depth={depth} value={current!r} label={label} scan={base.state.scan_id} ===")
        if depth >= max_depth:
            continue

        for action in (None, *actions):
            edge, trial_end = _run_trial(
                base=base,
                target=target,
                action=action,
                max_scans=max_scans,
                pdg=pdg,
                animate_rotate_sensor=animate_rotate_sensor,
            )
            if edge is None:
                print(f"  no edge via {_format_action(action)} within {max_scans} scans")
                continue
            graph_key = (edge.from_value, edge.cause)
            if graph_key in graph:
                print(
                    f"  duplicate edge via {edge.cause}: "
                    f"{edge.from_value!r}->{edge.to_value!r}"
                )
            else:
                graph[graph_key] = edge
                _print_edge(edge)
                _print_sibling_groups(edge, pdg)
                if stop_after_first_edge:
                    return list(graph.values())
            if edge.to_value not in queued_values:
                queued_values.add(edge.to_value)
                frontier.append((depth + 1, trial_end, edge.cause))

    return list(graph.values())


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--target", default="S_StateCurrent")
    parser.add_argument("--action", action="append", type=_parse_action, dest="actions")
    parser.add_argument("--no-default-actions", action="store_true")
    parser.add_argument("--max-depth", type=int, default=6)
    parser.add_argument("--max-scans", type=int, default=900)
    parser.add_argument("--animate-rotate-sensor", action="store_true", default=True)
    parser.add_argument(
        "--stop-after-first-edge",
        action="store_true",
        help="stop after one observed edge; useful for representative-structure learning",
    )
    args = parser.parse_args()

    pdg = build_program_graph(logic)
    actions = tuple(args.actions or ())
    if not args.no_default_actions:
        actions = (*DEFAULT_ACTIONS, *actions)

    print(f"CLICK_PROJECT={CLICK_PROJECT}")
    print(f"target={args.target} max_depth={args.max_depth} max_scans={args.max_scans}")
    print(f"actions={', '.join(_format_action(action) for action in actions)}")

    root = PLC(logic, record_all_tags=True)
    _setup_burner(root)
    print(f"root scan={root.state.scan_id} {args.target}={root.state.tags.get(args.target)!r}")

    edges = _learn_graph(
        root=root,
        target=args.target,
        actions=actions,
        max_depth=args.max_depth,
        max_scans=args.max_scans,
        pdg=pdg,
        animate_rotate_sensor=args.animate_rotate_sensor,
        stop_after_first_edge=args.stop_after_first_edge,
    )

    print("\n=== Transition Graph Summary ===")
    for edge in sorted(edges, key=lambda e: (str(e.from_value), e.cause)):
        prereq_text = ", ".join(f"{tag}={value!r}" for tag, value in edge.prerequisites) or "-"
        print(
            f"  {edge.from_value!r} --{edge.cause}/{edge.scans} scans--> "
            f"{edge.to_value!r}; prereqs: {prereq_text}; "
            f"local_delta={len(edge.participating_delta)} external_delta={edge.external_delta_count}"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
