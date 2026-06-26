"""Dump the trace tree for y_BurnerLoop at the Starting state.

Drives the burner to Starting (Clear->Reset->Start) and prints the backward
trace, so we can see which writer S_StateCompleteBool resolves through and
whether Blower__init / Rotate__init surface as the frontier.
"""

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
from pyrung.core.analysis.pilot.compass import detect_opaque_loop  # noqa: E402
from pyrung.core.analysis.pilot.evidence import (  # noqa: E402
    build_transition_evidence,
    infer_pipeline_roles,
)
from pyrung.core.analysis.pilot.physical import install_harness  # noqa: E402
from pyrung.core.analysis.pilot.pilot import _build_pilot_context  # noqa: E402
from pyrung.core.analysis.pilot.trace import (  # noqa: E402
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


def _pulse(plc: PLC, tag: str, settle: int = 8) -> None:
    plc.patch({tag: True})
    plc.step()
    for _ in range(settle):
        plc.step()


def _dump(node: Any, snap: dict[str, Any], *, indent: int = 0, limit: int = 120) -> int:
    flags = []
    if node.satisfied:
        flags.append("satisfied")
    if node.is_steerable:
        flags.append("steerable")
    if getattr(node, "pipeline_internal", False):
        flags.append("internal")
    if node.writer_rung is not None:
        flags.append(f"w={node.writer_rung}")
    suffix = f" [{' '.join(flags)}]" if flags else ""
    print(f"{'  ' * indent}- {node.tag}={node.value!r} have={snap.get(node.tag)!r}{suffix}")
    count = 1
    for child in node.children:
        if count >= limit:
            print(f"{'  ' * (indent + 1)}...")
            break
        count += _dump(child, snap, indent=indent + 1, limit=limit - count)
    return count


def main() -> int:
    plc = PLC(logic)
    for tag, value in PHYSICAL_PERMISSIVES.items():
        if _known(plc, tag):
            plc.force(tag, value)
    plc.step()

    pdg = build_program_graph(logic)
    harness_fb = install_harness(plc)
    ref_consts = compute_reference_constants(pdg, logic)
    steerable = compute_steerable(pdg, plc._known_tags_by_name, logic) - harness_fb - ref_consts
    opaque_loop = detect_opaque_loop(pdg, logic)

    _nd, _kc, evidence = _build_pilot_context(logic, dict(plc.state.tags))
    if evidence is None:
        evidence = build_transition_evidence(None)

    roles = []
    for tag in sorted(opaque_loop):
        if evidence is not None and not evidence.is_stepping(tag):
            continue
        role = infer_pipeline_roles(tag, pdg, logic, steerable, opaque_loop, evidence)
        if role.request_tags:
            roles.append(role)
    pit = frozenset(t for r in roles for t in r.trace_internal_tags)
    print(f"opaque_loop={sorted(opaque_loop)}")
    print(f"pipeline_internal_tags={sorted(pit)}")

    for cmd in ("C_ProductionMode", "C_Clear", "C_Reset", "C_Start"):
        _pulse(plc, cmd)
        snap = plc.state.tags
        print(f"after {cmd}: S_StateCurrent={snap.get('S_StateCurrent')} S_Starting={snap.get('S_Starting')}")

    snap = dict(plc.state.tags)

    # Which writer does S_StateCompleteBool resolve to now?
    from pyrung.core.analysis.pilot.trace import _rank_writers, _writer_state_affinity
    from pyrung.core.analysis.pdg import resolve_rung

    scb_writers = pdg.writers_of.get("S_StateCompleteBool", frozenset())
    print(f"\nS_StateCompleteBool writers + affinity (snapshot at Starting):")
    for ri in sorted(scb_writers):
        ro = resolve_rung(logic, pdg.rung_nodes[ri])
        if ro is None:
            continue
        print(f"  rung {ri}: affinity={_writer_state_affinity(ro, snap)}")
    ranked = _rank_writers(scb_writers, pdg, logic, "S_StateCompleteBool", 1, snap)
    print(f"  ranked order: {ranked}")

    print("\n=== trace y_BurnerLoop at Starting ===")
    tree = trace_back(
        "y_BurnerLoop", True, snap, pdg, logic, steerable,
        opaque_loop=opaque_loop, pipeline_internal_tags=pit, choice=None,
    )
    _dump(tree, snap)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
