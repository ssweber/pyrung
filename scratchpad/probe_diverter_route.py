"""Probe: why DiverterCmd is reported ambiguous instead of picking the
fully-steerable manual arm.

DiverterCmd rung:
    rung(EstopOK, Or(And(State==SORTING, IsLarge, Auto), And(Manual, DiverterBtn)))
       -> out(DiverterCmd)

Manual arm = And(Manual, DiverterBtn) — both directly-steerable inputs (under
the EstopOK gate). Auto arm needs internal State==SORTING + IsLarge latch.

Questions:
  1. What does pilot_how(DiverterCmd) actually return? (reachable/ambiguous/choices)
  2. What does enumerate_trace_choices surface? (the two routes)
  3. Does _or_ambiguity_over_inputs collapse it? (expect False today)
  4. If we hand-trace_back, does it land on the manual arm on its own?
"""

from __future__ import annotations

import os

os.environ.setdefault("PYRUNG_DAP_ACTIVE", "1")

import math

from pyrung.core.runner import PLC
from pyrung.core.analysis.pdg import build_program_graph, resolve_rung
from pyrung.core.analysis.pilot import pilot_how
from pyrung.core.analysis.pilot.trace import (
    compute_steerable,
    compute_reference_constants,
    trace_back,
    enumerate_trace_choices,
    _or_ambiguity_over_inputs,
    _rank_writers,
)
from pyrung.core.analysis.pilot.compass import detect_opaque_loop
from pyrung.core.analysis.pilot.physical import install_harness

from examples import click_conveyor as cv


def setup(plc):
    program = plc._program
    fork = plc.fork(history_budget=math.inf)
    pdg = build_program_graph(program)
    hb = install_harness(fork)
    rc = compute_reference_constants(pdg, program)
    steerable = compute_steerable(pdg, fork._known_tags_by_name, program) - hb - rc
    opaque_loop = detect_opaque_loop(pdg, program)
    return program, fork, pdg, steerable, opaque_loop


def _dump(node, snap, depth=0, seen=None):
    if seen is None:
        seen = set()
    pad = "  " * depth
    fl = []
    if node.satisfied:
        fl.append("SAT")
    if node.is_steerable:
        fl.append("STEER")
    if getattr(node, "pipeline_internal", False):
        fl.append("PIPE")
    print(f"{pad}{node.tag}={node.value!r} (cur={snap.get(node.tag)!r}) {' '.join(fl)}"
          + (f" <{node.data_flow}>" if node.data_flow else ""))
    if id(node) in seen or depth > 12:
        return
    seen.add(id(node))
    for c in node.children:
        _dump(c, snap, depth + 1, seen)


def main():
    plc = PLC(cv.logic, dt=0.010)
    plc.step()  # commit initial state
    program, fork, pdg, steerable, opaque_loop = setup(plc)
    snap = dict(fork.state.tags)

    print("=== current snapshot (relevant tags) ===")
    for t in ("EstopOK", "Manual", "Auto", "DiverterBtn", "State", "IsLarge", "DiverterCmd"):
        print(f"  {t} = {snap.get(t)!r}")

    print("\n=== 'DiverterCmd' in steerable? ===", "DiverterCmd" in steerable)
    print("    Manual steerable?", "Manual" in steerable,
          " DiverterBtn steerable?", "DiverterBtn" in steerable,
          " IsLarge steerable?", "IsLarge" in steerable,
          " State steerable?", "State" in steerable)

    print("\n=== writers of DiverterCmd ===")
    for ri in sorted(pdg.writers_of.get("DiverterCmd", frozenset())):
        rn = pdg.rung_nodes[ri]
        ro = resolve_rung(program, rn)
        print(f"  node {ri}: rung={rn.rung_index} cond={ro.sp_tree() if ro else None}")
        print(f"           ote_writes={rn.ote_writes}")

    print("\n=== _rank_writers(DiverterCmd=True) ===")
    ranked = _rank_writers(
        pdg.writers_of.get("DiverterCmd", frozenset()),
        pdg, program, "DiverterCmd", True, snap,
    )
    print(f"  {ranked}")

    print("\n=== _or_ambiguity_over_inputs(writer, DiverterCmd=True) ===")
    for ri in ranked:
        collapses = _or_ambiguity_over_inputs(
            ri, "DiverterCmd", True, snap, pdg, program, steerable
        )
        print(f"  writer {ri}: collapses={collapses}")

    print("\n=== enumerate_trace_choices('DiverterCmd', True) ===")
    choices = enumerate_trace_choices(
        "DiverterCmd", True, snap, pdg, program, steerable=steerable
    )
    print(f"  {len(choices)} choice(s)")
    for ch in choices:
        print(f"    id={ch.id} label={ch.label!r} route={ch.route}")

    print("\n=== trace_back('DiverterCmd', True) [no choice lock] ===")
    t = trace_back("DiverterCmd", True, snap, pdg, program, steerable, opaque_loop=opaque_loop)
    _dump(t, snap)
    print("  ordered_actions:", t.ordered_actions())

    print("\n=== pilot_how(DiverterCmd) ===")
    plc2 = PLC(cv.logic, dt=0.010)
    path = pilot_how(plc2, cv.DiverterCmd, max_scans=500)
    print(f"  reachable={path.reachable}")
    print(f"  ambiguous={getattr(path, 'ambiguous', '?')}")
    print(f"  choices={[c.label for c in getattr(path, 'choices', [])]}")
    print(f"  steps={[(s.action, s.scans) for s in path.steps]}")


if __name__ == "__main__":
    main()
