"""Probe #3: would a *corrected* value-graph chain the command hops?

Build edges from expand_routes('StateCurrent') but FIX the two defects:
  D2a: expand OR-valued / missing 'from' state -> multiple from_values
       (recovered from the route's writer condition StateCurrent values).
  D2b: bridge the non-steerable CtrlCmd==N enabler -> steerable Cmd* button
       via expand_routes('CtrlCmd').  Add rise(CmdChgRequest) as co-action.

Then forward-BFS current->target and print the path + first hop.  If this
chains STOPPED->IDLE and ABORTED->EXECUTE, the value-graph extension is
sufficient (no sandbox needed).
"""

from __future__ import annotations

import math
from collections import deque

from pyrung.core.runner import PLC
from pyrung.core.analysis.pdg import build_program_graph, resolve_rung
from pyrung.core.analysis.simplified import _sp_to_expr
from pyrung.core.analysis.sp_values import _extract_condition_values
from pyrung.core.analysis.pilot.trace import compute_steerable, compute_reference_constants
from pyrung.core.analysis.pilot.compass import detect_opaque_loop
from pyrung.core.analysis.pilot.evidence import expand_routes
from pyrung.core.analysis.pilot.physical import install_harness

from examples.packml_bench import (
    S, StateCurrent, logic,
    CmdAbort, CmdChgRequest,
)

STATE_NAME = {v: k for k, v in {
    "CLEARING":1,"STOPPED":2,"STARTING":3,"IDLE":4,"SUSPENDED":5,"EXECUTE":6,
    "STOPPING":7,"ABORTING":8,"ABORTED":9,"HOLDING":10,"HELD":11,"UNHOLDING":12,
    "SUSPENDING":13,"UNSUSPENDING":14,"RESETTING":15,"COMPLETING":16,"COMPLETED":17,
}.items()}


def setup(plc):
    program = plc._program
    fork = plc.fork(history_budget=math.inf)
    pdg = build_program_graph(program)
    hb = install_harness(fork)
    rc = compute_reference_constants(pdg, program)
    steerable = compute_steerable(pdg, fork._known_tags_by_name, program) - hb - rc
    opaque_loop = detect_opaque_loop(pdg, program)
    return program, fork, pdg, steerable, opaque_loop


def writer_state_values(pdg, program, node_idx, tag="StateCurrent"):
    """All StateCurrent values appearing in this writer's own condition."""
    ro = resolve_rung(program, pdg.rung_nodes[node_idx])
    if ro is None:
        return set()
    sp = ro.sp_tree()
    if sp is None:
        return set()
    cond = _extract_condition_values(_sp_to_expr(sp))
    return set(cond.get(tag, ()))


def build_ctrlcmd_bridge(pdg, program, steerable, opaque_loop):
    """CtrlCmd value -> steerable button action."""
    bridge = {}
    for r in expand_routes("CtrlCmd", pdg, program, steerable, opaque_loop, None):
        for tag, val in r.enablers:
            if tag in steerable:
                bridge[r.destination_value] = (tag, val)
    return bridge


def build_fixed_edges(pdg, program, steerable, opaque_loop):
    bridge = build_ctrlcmd_bridge(pdg, program, steerable, opaque_loop)
    routes = expand_routes("StateCurrent", pdg, program, steerable, opaque_loop, None)
    edges = []  # (from_value, to_value, actions, kind)
    for r in routes:
        if r.destination_value is None:
            continue
        dest = r.destination_value
        # from-states: prefer source_constraints on StateCurrent, else recover
        # from the writer's own condition (handles OR-valued / dropped sources).
        froms = [v for t, v in r.source_constraints if t == "StateCurrent"]
        if not froms:
            froms = sorted(writer_state_values(pdg, program, r.writer_node))
        # actions: bridge CtrlCmd enablers -> buttons; completion edges have none.
        actions = []
        is_command = False
        for t, v in r.enablers:
            if t == "CtrlCmd":
                is_command = True
                if v in bridge:
                    actions.append(bridge[v])
            elif t in steerable:
                actions.append((t, v))
        if is_command:
            actions.append(("CmdChgRequest", True))  # rise gate at call site
        kind = "cmd" if is_command else ("complete" if not r.enablers else "other")
        # PRECISION: only navigable state->state transitions.  A route with no
        # concrete StateCurrent from-state is an init/clear/fault rung (the
        # ~InitDone seed, the LoopIndex>10 runaway guard, the StateRequested=0
        # clear) — NOT a transition.  Dropping these kills the phantom
        # `* -> STOPPED` shortcut that let ABORTED skip its CmdClear hop.
        if not froms:
            continue
        for fv in froms:
            edges.append((fv, dest, tuple(actions), kind))
    return edges


def bfs(edges, start, target):
    adj = {}
    for fv, tv, acts, kind in edges:
        adj.setdefault(fv, []).append((tv, acts, kind))
    adj.setdefault("*", [])
    q = deque([(start, [])])
    seen = {start}
    while q:
        s, path = q.popleft()
        outs = adj.get(s, []) + adj.get("*", [])
        for tv, acts, kind in outs:
            if tv in seen:
                continue
            np = path + [(s, tv, acts, kind)]
            if tv == target:
                return np
            seen.add(tv)
            q.append((tv, np))
    return None


def show(edges, start, target, label):
    print(f"\n=== BFS {label}: {STATE_NAME.get(start)}({start}) -> {STATE_NAME.get(target)}({target}) ===")
    path = bfs(edges, start, target)
    if path is None:
        print("  NO PATH")
        return
    for fv, tv, acts, kind in path:
        print(f"  {STATE_NAME.get(fv,fv)}({fv}) --{kind}:{list(acts)}--> {STATE_NAME.get(tv)}({tv})")
    print(f"  NEXT HOP action(s): {list(path[0][2]) or '(coast)'}")


def main():
    plc = PLC(logic, dt=0.010)
    plc.step()
    program, fork, pdg, steerable, opaque_loop = setup(plc)
    edges = build_fixed_edges(pdg, program, steerable, opaque_loop)

    print("=== corrected edges (command + completion) ===")
    for fv, tv, acts, kind in sorted(edges, key=lambda e: (str(e[0]), e[1])):
        if kind in ("cmd", "complete"):
            print(f"  {STATE_NAME.get(fv,fv):>11}({fv}) --{kind:8s} {list(acts)}--> {STATE_NAME.get(tv)}({tv})")

    show(edges, S.STOPPED.default, S.IDLE.default, "test1 idle")
    show(edges, S.ABORTED.default, S.EXECUTE.default, "test2 execute")


if __name__ == "__main__":
    main()
