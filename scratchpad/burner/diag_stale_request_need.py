"""Inspect why S_StateRequested=2 remains in the trace after Stopped."""

from __future__ import annotations

import os
import sys
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
from pyrung.core.analysis.pdg import build_program_graph  # noqa: E402
from pyrung.core.analysis.pilot.evidence import build_transition_evidence, expand_routes  # noqa: E402
from pyrung.core.analysis.pilot.compass import Compass  # noqa: E402
from pyrung.core.analysis.pilot.physical import install_harness  # noqa: E402
from pyrung.core.analysis.pilot.pilot import _build_pilot_context  # noqa: E402
from pyrung.core.analysis.pilot.trace import (  # noqa: E402
    compute_edge_tags,
    compute_reference_constants,
    compute_steerable,
    trace_back,
)


PHYSICAL_PERMISSIVES = {
    "x_DoorClosed": True,
    "x_LintDoorClosed": True,
    "x_BlowerFB": True,
    "x_RotateFB": True,
    "x_RotateSensor": False,
    "x_SailRelay": True,
}


def _known(plc: PLC, tag: str) -> bool:
    return tag in plc._known_tags_by_name or tag in plc.state.tags


def _step(plc: PLC) -> None:
    plc.step()


def _pulse(plc: PLC, tag: str, settle: int = 4) -> None:
    plc.patch({tag: True})
    _step(plc)
    for _ in range(settle):
        _step(plc)


def _dump_trace(node: Any, snap: dict[str, Any], *, indent: int = 0, limit: int = 80) -> int:
    prefix = "  " * indent
    flags = []
    if node.satisfied:
        flags.append("satisfied")
    if node.is_steerable:
        flags.append("steerable")
    if node.data_flow:
        flags.append(f"data={node.data_flow}")
    if node.writer_rung is not None:
        flags.append(f"writer={node.writer_rung}")
    suffix = f" [{' '.join(flags)}]" if flags else ""
    print(f"{prefix}- {node.tag}={node.value!r} have={snap.get(node.tag)!r}{suffix}")
    count = 1
    for child in node.children:
        if count >= limit:
            print(f"{prefix}  ...")
            return count
        count += _dump_trace(child, snap, indent=indent + 1, limit=limit - count)
    return count


def main() -> int:
    plc = PLC(logic)
    for tag, value in PHYSICAL_PERMISSIVES.items():
        if _known(plc, tag):
            plc.force(tag, value)
    _step(plc)

    pdg = build_program_graph(logic)
    harness_fb = install_harness(plc)
    ref_consts = compute_reference_constants(pdg, logic)
    steerable = compute_steerable(pdg, plc._known_tags_by_name, logic) - harness_fb - ref_consts
    opaque_loop = frozenset({"S_StateCurrent", "S_StateRequested"})

    nd_domains, _key_config, evidence = _build_pilot_context(logic, dict(plc.state.tags))
    del nd_domains
    if evidence is None:
        evidence = build_transition_evidence(None)

    routes = expand_routes("S_StateCurrent", pdg, logic, steerable, opaque_loop, evidence)
    inf = Compass()
    inf.seed_routes("S_StateCurrent", routes)

    print("Seeded routes touching stopped/reset/start:")
    for route in routes:
        if route.destination_value in {2, 3, 4, 6, 15}:
            print(
                f"  {route.source_constraints} -- {route.enablers} "
                f"-> req {route.request_value!r} dest {route.destination_value!r}"
            )

    _pulse(plc, "C_ProductionMode")
    _pulse(plc, "C_Abort")
    _pulse(plc, "C_Clear")

    snap = dict(plc.state.tags)
    print("\nAfter production/abort/clear:")
    for tag in ("S_StateCurrent", "S_StateRequested", "S_Stopped", "C_CtrlCmd"):
        print(f"  {tag}={snap.get(tag)!r}")

    print("\nInfluence paths:")
    for src in (2, 4, 9):
        print(f"  S_StateCurrent {src}->6: {inf.find_path('S_StateCurrent', src, 6)}")
    for action in (("C_Clear", True), ("C_Reset", True), ("C_Start", True)):
        print(
            f"  dest from 2 via {action}: "
            f"{inf.transition_dest('S_StateCurrent', 2, action)!r}"
        )

    tree = trace_back(
        "y_BurnerLoop",
        True,
        snap,
        pdg,
        logic,
        steerable,
        opaque_loop=opaque_loop,
        choice=None,
    )
    print("\nTrace after stopped:")
    _dump_trace(tree, snap)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
