"""Probe #2: writer ranking, CtrlCmd bridge, and ground-truth forward hops.

Answers:
  - Which writer of StateRequested does _rank_writers pick (command vs jump-chain)?
  - Does _find_convergent_steers('CtrlCmd') recover the Cmd* buttons?
  - Ground truth: from STOPPED, what command pulses actually advance StateCurrent?
  - If we trace StateRequested=15 directly (RESETTING), do we reach a button?
"""

from __future__ import annotations

import math

from pyrung.core.runner import PLC
from pyrung.core.analysis.pdg import build_program_graph, resolve_rung
from pyrung.core.analysis.pilot.trace import (
    compute_steerable, compute_reference_constants, trace_back, _rank_writers, _all_nodes,
)
from pyrung.core.analysis.pilot.compass import detect_opaque_loop, _find_convergent_steers
from pyrung.core.analysis.pilot.evidence import expand_routes
from pyrung.core.analysis.pilot.physical import install_harness

from examples.packml_bench import (
    S, StateCurrent, logic,
    CmdReset, CmdStart, CmdClear, CmdChgRequest, CmdAbort,
)


def setup(plc):
    program = plc._program
    fork = plc.fork(history_budget=math.inf)
    pdg = build_program_graph(program)
    hb = install_harness(fork)
    rc = compute_reference_constants(pdg, program)
    steerable = compute_steerable(pdg, fork._known_tags_by_name, program) - hb - rc
    opaque_loop = detect_opaque_loop(pdg, program)
    return program, fork, pdg, steerable, opaque_loop


def main():
    plc = PLC(logic, dt=0.010)
    plc.step()  # STOPPED
    program, fork, pdg, steerable, opaque_loop = setup(plc)
    snap = dict(fork.state.tags)

    print("=== writers of StateRequested ===")
    for ri in sorted(pdg.writers_of.get("StateRequested", frozenset())):
        rn = pdg.rung_nodes[ri]
        ro = resolve_rung(program, rn)
        sp = ro.sp_tree() if ro else None
        print(f"  node {ri}: sub={rn.subroutine} rung={rn.rung_index} cond={sp}")

    print("\n=== _rank_writers(StateRequested=4 [IDLE]) from STOPPED ===")
    ranked = _rank_writers(
        pdg.writers_of.get("StateRequested", frozenset()),
        pdg, program, "StateRequested", 4, snap, opaque_loop,
    )
    print(f"  ranked: {ranked}  (winner picks first)")
    for ri in ranked[:3]:
        rn = pdg.rung_nodes[ri]
        print(f"    node {ri}: sub={rn.subroutine} rung={rn.rung_index}")

    print("\n=== _rank_writers(StateRequested=15 [RESETTING]) from STOPPED ===")
    ranked = _rank_writers(
        pdg.writers_of.get("StateRequested", frozenset()),
        pdg, program, "StateRequested", 15, snap, opaque_loop,
    )
    print(f"  ranked: {ranked}")
    for ri in ranked[:3]:
        rn = pdg.rung_nodes[ri]
        print(f"    node {ri}: sub={rn.subroutine} rung={rn.rung_index}")

    print("\n=== _find_convergent_steers('CtrlCmd') ===")
    conv = _find_convergent_steers("CtrlCmd", pdg, steerable)
    print(f"  {sorted(conv)}")

    print("\n=== expand_routes('CtrlCmd') ===")
    for r in expand_routes("CtrlCmd", pdg, program, steerable, opaque_loop, None):
        print(f"   dest={r.destination_value!r} enablers={r.enablers} actions={sorted(r.action_tags)}")

    print("\n=== backward trace: StateRequested==15 (RESETTING) directly from STOPPED ===")
    t = trace_back("StateRequested", 15, snap, pdg, program, steerable, opaque_loop=opaque_loop)
    _dump(t, snap)
    print("  ordered_actions:", t.ordered_actions())

    print("\n=== backward trace: CtrlCmd==1 (Reset) directly ===")
    t = trace_back("CtrlCmd", 1, snap, pdg, program, steerable, opaque_loop=opaque_loop)
    _dump(t, snap)
    print("  ordered_actions:", t.ordered_actions())

    print("\n=== GROUND TRUTH forward hops from STOPPED ===")
    _ground_truth()


def _ground_truth():
    plc = PLC(logic, dt=0.010)
    plc.step()
    print(f"  start StateCurrent={plc.state.tags['StateCurrent']} (STOPPED=2)")

    def pulse(label, patch):
        plc.patch(patch)
        plc.step()
        sc = plc.state.tags["StateCurrent"]
        sr = plc.state.tags["StateRequested"]
        cv = plc.state.tags.get("CmdValidYes")
        print(f"  after {label:30s}: StateCurrent={sc} StateRequested={sr} CmdValidYes={cv}")

    # STOPPED -> RESETTING -> IDLE
    pulse("CmdReset+CmdChgRequest", {CmdReset: True, CmdChgRequest: True})
    pulse("(clear, let auto-complete)", {CmdReset: False, CmdChgRequest: False})
    pulse("(idle scan)", {})
    pulse("(idle scan)", {})
    # IDLE -> STARTING -> EXECUTE
    pulse("CmdStart+CmdChgRequest", {CmdStart: True, CmdChgRequest: True})
    pulse("(clear)", {CmdStart: False, CmdChgRequest: False})
    pulse("(idle scan)", {})
    pulse("(idle scan)", {})


def _dump(node, snap, depth=0, seen=None):
    if seen is None:
        seen = set()
    pad = "  " * depth
    fl = []
    if node.satisfied: fl.append("SAT")
    if node.is_steerable: fl.append("STEER")
    if getattr(node, "pipeline_internal", False): fl.append("PIPE")
    print(f"{pad}{node.tag}={node.value!r} (cur={snap.get(node.tag)!r}) {' '.join(fl)}"
          + (f" <{node.data_flow}>" if node.data_flow else ""))
    if id(node) in seen or depth > 10:
        return
    seen.add(id(node))
    for c in node.children:
        _dump(c, snap, depth + 1, seen)


if __name__ == "__main__":
    main()
