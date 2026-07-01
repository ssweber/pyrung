"""Prototype: resting-value preserve + cross-target contradiction scan.

Confirms the load-bearing claim from the Pair 3 pressure-test:
  - running preserve on a RESTING held value (State==IDLE, already satisfied)
    surfaces the suppression leaf EntrySensor=False, which today's trace never
    emits (it short-circuits satisfied values before the writer/preserve loop).
  - that leaf collides with IsLarge's AND-required EntrySensor=True → a
    statically-readable mutual-exclusion signal.

This lives entirely in the probe — no edit to trace.py — by reusing its
module-level internals to replicate the _preserve_children loop with the
establish-writer exclusion + ote gate removed (the resting case).
"""

import os

os.environ["PYTHONPATH"] = "."
os.environ["PYRUNG_DAP_ACTIVE"] = "1"

import math

from examples import click_conveyor as cv
from pyrung.core.runner import PLC
from pyrung.core.analysis.pdg import build_program_graph
from pyrung.core.analysis.pilot.physical import install_harness
from pyrung.core.analysis.pilot import trace as T


def _setup():
    plc = PLC(cv.logic, dt=0.010)
    program = plc._program
    fork = plc.fork(history_budget=math.inf)
    pdg = build_program_graph(program)
    harness_fb = install_harness(fork)
    ref_consts = T.compute_reference_constants(pdg, program)
    steerable = (
        T.compute_steerable(pdg, fork._known_tags_by_name, program) - harness_fb - ref_consts
    )
    snapshot = dict(fork.state.tags)
    env = T._env_for(snapshot, pdg, program, steerable)
    return env, snapshot, steerable


def resting_preserve(env, tag, value):
    """PROTOTYPE resting-value preserve.

    Body of trace._preserve_children with the establish-writer exclusion and the
    ote-gate removed: for a value that is simply *resting* (no establishing
    writer), surface the negation of every writer that provably drives the tag
    off `value`.  Returns the suppression prereq children.
    """
    children = []
    seen = set()
    for ri in sorted(env.pdg.writers_of.get(tag, frozenset())):
        rn = env.pdg.rung_nodes[ri]
        ro = T.resolve_rung(env.program, rn)
        if ro is None:
            continue
        # A clobber = a writer that provably drives the tag AWAY from value.
        if T._can_produce(T._written_value_for_tag(ro, tag), value):
            continue
        sp = ro.sp_tree()
        if sp is None:
            continue
        suppress = T._negate(T._sp_to_expr(sp))
        key = T._expr_route_key(suppress)
        if key in seen:
            continue
        seen.add(key)
        children.extend(
            T._trace_expression(
                env,
                suppress,
                tag,
                provenance=(T._scope_ref(ri, rn),),
                _visited=set(),
                _ancestry=(),
                _depth=0,
            )
        )
    return children


def steer_leaves(nodes):
    out = []
    for n in nodes:
        out.extend(n.steerable_leaves())
    return out


def contradiction_scan(a_demands, b_demands, label_a, label_b):
    """Find a steerable tag forced to two different values across the two demand
    sets (A's establish demands vs B's hold/preserve demands)."""
    a_by_tag = {}
    for t, v in a_demands:
        a_by_tag.setdefault(t, set()).add(v)
    hits = []
    for t, v in b_demands:
        if t in a_by_tag and v not in a_by_tag[t]:
            hits.append((t, sorted(a_by_tag[t]), v))
    print(f"\n  contradiction scan: {label_a} establish  vs  hold {label_b}")
    if not hits:
        print("    (no opposite-value collision)")
    for t, avals, bval in hits:
        print(f"    !! {t}: {label_a} requires {avals}, holding {label_b} requires {bval}")
    return hits


def main():
    env, snapshot, steerable = _setup()
    print("State resting value @cold:", snapshot.get("State"))

    print("\n--- resting_preserve(State == IDLE(0)) ---")
    rp = resting_preserve(env, "State", 0)
    for n in rp:
        print("   leaf:", n.tag, "=", repr(n.value), "steerable" if n.is_steerable else "")
    rp_leaves = sorted(set(steer_leaves(rp)))
    print("   resting-preserve steerable leaves:", rp_leaves)

    print("\n--- establish trace: IsLarge == True ---")
    islarge = T.trace_back("IsLarge", True, snapshot, env.pdg, env.program, steerable)
    il_leaves = sorted(set(islarge.steerable_leaves()))
    print("   IsLarge steerable leaves:", il_leaves)

    # ME needs BOTH orderings to contradict; here we demonstrate the direction
    # the pressure-test was about (establish IsLarge while holding State==IDLE).
    contradiction_scan(set(il_leaves), set(rp_leaves), "IsLarge", "State==IDLE")

    # Control: a pair that should NOT be ME — Motor establish vs holding State==IDLE.
    print("\n--- control: ConveyorMotor establish vs holding State==IDLE ---")
    motor = T.trace_back("ConveyorMotor", True, snapshot, env.pdg, env.program, steerable)
    m_leaves = sorted(set(motor.steerable_leaves()))
    print("   Motor steerable leaves:", m_leaves)
    contradiction_scan(set(m_leaves), set(rp_leaves), "ConveyorMotor", "State==IDLE")


if __name__ == "__main__":
    main()
