"""Probe: what does the PILOT compass see for StateCurrent in packml_bench?

Reconstructs the pilot_how() setup and dumps every instrument's view of the
StateCurrent jump-table state machine, to answer:
  - Is StateCurrent in opaque_loop?  Is it 'stepping' per evidence?
  - Does a PipelineRole get inferred for StateCurrent?
  - What routes does expand_routes("StateCurrent") find?  destination_values?
  - Do compass graphs build edges?  Can best_compass_plan reach IDLE?
  - Where does the backward trace tree dead-end?
"""

from __future__ import annotations

import math

from pyrung.core.runner import PLC
from pyrung.core.analysis.pdg import build_program_graph
from pyrung.core.analysis.pilot.trace import (
    compute_steerable,
    compute_edge_tags,
    compute_resting_values,
    compute_reference_constants,
    trace_back,
    _all_nodes,
)
from pyrung.core.analysis.pilot.compass import (
    Compass,
    detect_opaque_loop,
    detect_opaque_pipelines,
    best_compass_plan,
)
from pyrung.core.analysis.pilot.evidence import expand_routes, infer_pipeline_roles
from pyrung.core.analysis.pilot.physical import install_harness

from examples.packml_bench import S, StateCurrent, logic


def build_setup(plc):
    program = plc._program
    fork = plc.fork(history_budget=math.inf)
    pdg = build_program_graph(program)
    harness_fb = install_harness(fork)
    ref_consts = compute_reference_constants(pdg, program)
    steerable = compute_steerable(pdg, fork._known_tags_by_name, program) - harness_fb - ref_consts
    edge_tags = compute_edge_tags(pdg, program)

    # prover context (nd_domains + evidence) — same as _build_pilot_context
    from pyrung.core.analysis.pilot.pilot import _build_pilot_context
    nd_domains, key_config, evidence = _build_pilot_context(program, dict(fork.state.tags))

    opaque_loop = detect_opaque_loop(pdg, program)
    return dict(
        program=program, fork=fork, pdg=pdg, steerable=steerable,
        edge_tags=edge_tags, evidence=evidence, nd_domains=nd_domains,
        opaque_loop=opaque_loop,
    )


def dump_tree(node, snap, depth=0, seen=None):
    if seen is None:
        seen = set()
    pad = "  " * depth
    flags = []
    if node.satisfied:
        flags.append("SAT")
    if node.is_steerable:
        flags.append("STEER")
    if getattr(node, "pipeline_internal", False):
        flags.append("PIPE")
    if getattr(node, "self_advancing", False):
        flags.append("COAST")
    if getattr(node, "relational", False):
        flags.append("REL")
    cur = snap.get(node.tag)
    line = f"{pad}{node.tag}={node.value!r} (cur={cur!r}) {' '.join(flags)}"
    if node.writer_rung is not None:
        line += f" [w:{node.writer_rung}]"
    if node.data_flow:
        line += f" <{node.data_flow}>"
    print(line)
    key = id(node)
    if key in seen:
        print(f"{pad}  ...(repeat)")
        return
    seen.add(key)
    if depth > 12:
        print(f"{pad}  ...(depth cap)")
        return
    for c in node.children:
        dump_tree(c, snap, depth + 1, seen)


def main():
    plc = PLC(logic, dt=0.010)
    plc.step()  # init → STOPPED
    print("=" * 70)
    print(f"Seeded StateCurrent = {plc.state.tags['StateCurrent']} (expect STOPPED=2)")
    print("=" * 70)

    s = build_setup(plc)
    pdg, program, steerable, evidence = s["pdg"], s["program"], s["steerable"], s["evidence"]
    opaque_loop = s["opaque_loop"]

    print("\n--- opaque_loop ---")
    print(sorted(opaque_loop))
    print(f"StateCurrent in opaque_loop: {'StateCurrent' in opaque_loop}")
    print(f"StateRequested in opaque_loop: {'StateRequested' in opaque_loop}")

    print("\n--- evidence classification ---")
    if evidence is None:
        print("evidence is None!")
    else:
        for t in ["StateCurrent", "StateRequested", "CtrlCmd", "StateCompleteBool",
                  "CmdValidYes", "StateEnableYes", "LoopIndex"]:
            print(f"  {t}: classify={evidence.classify(t)} stepping={evidence.is_stepping(t)}")

    print("\n--- steerable (cmd-relevant) ---")
    print(sorted(t for t in steerable if "Cmd" in t or "Mode" in t))

    print("\n--- pipeline roles for StateCurrent ---")
    role = infer_pipeline_roles("StateCurrent", pdg, program, steerable, opaque_loop, evidence)
    print(f"  governing_tag={role.governing_tag}")
    print(f"  request_tags={sorted(role.request_tags)}")
    print(f"  guard_internal={sorted(role.guard_internal_tags)}")
    print(f"  scratch_internal={sorted(role.scratch_internal_tags)}")

    print("\n--- _infer_pipeline_roles_for_context gate ---")
    # The actual gate that decides whether a CompassGraph gets built:
    print(f"  opaque_loop nonempty: {bool(opaque_loop)}")
    for tag in sorted(opaque_loop):
        is_step = evidence is not None and evidence.is_stepping(tag)
        r = infer_pipeline_roles(tag, pdg, program, steerable, opaque_loop, evidence)
        gated_in = is_step and bool(r.request_tags)
        if "State" in tag or gated_in:
            print(f"  {tag}: stepping={is_step} request_tags={sorted(r.request_tags)} -> role_built={gated_in}")

    print("\n--- expand_routes('StateCurrent') ---")
    routes = expand_routes("StateCurrent", pdg, program, steerable, opaque_loop, evidence)
    print(f"  {len(routes)} routes")
    for r in routes:
        print(f"    dest={r.destination_value!r} via req={r.request_tag}={r.request_value!r} "
              f"src={r.source_constraints} enablers={r.enablers} actions={sorted(r.action_tags)} "
              f"node={r.writer_node} sub={r.writer_subroutine}")

    print("\n--- expand_routes('StateRequested') ---")
    routes_req = expand_routes("StateRequested", pdg, program, steerable, opaque_loop, evidence)
    print(f"  {len(routes_req)} routes")
    for r in routes_req[:40]:
        print(f"    dest={r.destination_value!r} via req={r.request_tag}={r.request_value!r} "
              f"src={r.source_constraints} enablers={r.enablers} actions={sorted(r.action_tags)}")

    print("\n--- compass graphs + best_compass_plan(StateCurrent==IDLE) ---")
    from pyrung.core.analysis.pilot.pilot import (
        _infer_pipeline_roles_for_context,
        _build_compass_graphs_for_context,
    )
    roles = _infer_pipeline_roles_for_context(pdg, program, steerable, opaque_loop, evidence)
    print(f"  roles built: {[r.governing_tag for r in roles]}")
    graphs = _build_compass_graphs_for_context(roles, pdg, program, steerable, opaque_loop, evidence)
    print(f"  graphs: {[(g.role.governing_tag, len(g.edges)) for g in graphs]}")
    for g in graphs:
        print(f"  --- graph {g.role.governing_tag} edges ---")
        for e in g.edges[:50]:
            print(f"    {e.from_value!r} --{e.action}--> {e.to_value!r} "
                  f"(req={e.request_tag}={e.request_value!r} enablers={e.enablers})")

    snap = dict(s["fork"].state.tags)
    plan = best_compass_plan("StateCurrent", S.IDLE.default, snap, graphs)
    print(f"\n  best_compass_plan(StateCurrent==IDLE from STOPPED): {plan}")
    if plan is not None:
        for e in plan.edges:
            print(f"    edge {e.from_value!r}->{e.to_value!r} action={e.action}")

    print("\n--- backward trace tree: StateCurrent==IDLE from STOPPED ---")
    tree = trace_back(
        "StateCurrent", S.IDLE.default, snap, pdg, program, steerable,
        opaque_loop=opaque_loop,
    )
    dump_tree(tree, snap)
    print("\n  ordered_actions:", tree.ordered_actions())
    print("  unsatisfied_count:", tree.unsatisfied_count())
    print("  pivot_tags:", sorted(tree.pivot_tags()))


if __name__ == "__main__":
    main()
