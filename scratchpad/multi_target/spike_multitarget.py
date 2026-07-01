"""SPIKE: static multi-target ME classifier over the real pilot trace.

Feasibility question: can a *purely static* read produce the right reachable /
unreachable verdict for all four conveyor pairs + the control, with NO sandbox /
forward-sim?

Classifier (sound-prune, else fail-open):
  1. same-tag / different-value  -> ME (trivial pre-check).
  2. mutual retentive clobber    -> ME.  clobber(X->Y): establishing/holding X
     fires a rung that writes Y's tag off Y's value, RETENTIVELY.  "Relevant
     writers of X" = rungs on X's establish-trace  UNION  X's producers (writers
     that can produce X's value) — the union is what makes the resting/second-
     ordering direction visible (IDLE is satisfied at cold, but its producer is
     the RESETTING rung, which also reset(IsLarge)).
  3. otherwise                    -> reachable (compose / order; fail-open).

Prints, per pair, the verdict + the clobber evidence, so we can see whether the
read is right AND where it is doing an existential (any route) that a sound
version would need to make universal (all routes).
"""

import os

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
    return env, snapshot


def _all_writer_rungs(node, out):
    if node.writer_rung is not None:
        out.add(node.writer_rung)
    for ch in node.children:
        _all_writer_rungs(ch, out)


def _producers(env, tag, val):
    """Rungs that can produce (tag == val)."""
    out = set()
    for ri in env.pdg.writers_of.get(tag, frozenset()):
        ro = T.resolve_rung(env.program, env.pdg.rung_nodes[ri])
        if ro is None:
            continue
        if T._can_produce(T._written_value_for_tag(ro, tag), val):
            out.add(ri)
    return out


def _writes_off(env, ri, tag, val):
    """Does rung `ri` write `tag` to a value that can't produce `val`, retentively?
    Returns (True, written_value) if it is a retentive clobber, else (False, None)."""
    if ri not in env.pdg.writers_of.get(tag, frozenset()):
        return False, None
    ro = T.resolve_rung(env.program, env.pdg.rung_nodes[ri])
    if ro is None:
        return False, None
    wv = T._written_value_for_tag(ro, tag)
    if T._can_produce(wv, val):
        return False, None  # this writer could itself produce the target -> not a clobber
    retentive = tag not in env.pdg.rung_nodes[ri].ote_writes
    if not retentive:
        return False, None  # OTE / self-clearing -> transient, not a clobber
    return True, wv


def _relevant_writers(env, tag, val):
    """Rungs fired to establish/hold (tag == val): establish-trace rungs UNION producers."""
    node = T.trace_back(tag, val, env.snapshot, env.pdg, env.program, env.steerable)
    rungs = set()
    _all_writer_rungs(node, rungs)
    rungs |= _producers(env, tag, val)
    return rungs


def _clobber(env, x, y):
    """Establishing/holding X drives Y off-value retentively?  X, Y are (tag, val).
    Returns list of (ri, written_value) evidence."""
    xt, xv = x
    yt, yv = y
    hits = []
    for ri in sorted(_relevant_writers(env, xt, xv)):
        off, wv = _writes_off(env, ri, yt, yv)
        if off:
            hits.append((ri, wv))
    return hits


def _label(env, ri):
    rn = env.pdg.rung_nodes[ri]
    return T._scope_ref(ri, rn)


def classify(env, a, b, name_a, name_b):
    at, av = a
    bt, bv = b
    print("=" * 74)
    print(f"PAIR:  {name_a} ({at}={av!r})   +   {name_b} ({bt}={bv!r})")
    # 1. same-tag pre-check
    if at == bt and not T._values_match(av, bv):
        print(f"  VERDICT: UNREACHABLE  — same register {at}, two values {av!r}/{bv!r}")
        return
    # 2. mutual retentive clobber
    ab = _clobber(env, a, b)  # establishing A clobbers held B
    ba = _clobber(env, b, a)  # establishing B clobbers held A
    print(f"  clobber({name_a} -> {name_b}): "
          + (", ".join(f"{_label(env, ri)} writes {bt}={wv!r}" for ri, wv in ab) or "none"))
    print(f"  clobber({name_b} -> {name_a}): "
          + (", ".join(f"{_label(env, ri)} writes {at}={wv!r}" for ri, wv in ba) or "none"))
    if ab and ba:
        print("  VERDICT: UNREACHABLE  — mutual retentive clobber")
    elif ab or ba:
        first = name_b if ab else name_a  # clobbered one must be established last
        print(f"  VERDICT: REACHABLE (ordered)  — establish {first} last / hold it")
    else:
        print("  VERDICT: REACHABLE (compose)  — no clobber either direction")


def main():
    env, _ = _setup()
    MOTOR = ("ConveyorMotor", True)
    DIVERTER = ("DiverterCmd", True)
    IDLE = ("State", 0)
    SORTING = ("State", 2)
    ISLARGE = ("IsLarge", True)

    print("\n### PAIR 1 — two outputs (expect REACHABLE)")
    classify(env, MOTOR, DIVERTER, "Motor", "Diverter")
    print("\n### PAIR 2 — same-tag State values (expect UNREACHABLE, same-tag)")
    classify(env, IDLE, SORTING, "State==IDLE", "State==SORTING")
    print("\n### PAIR 3 — cross-tag ME (expect UNREACHABLE, mutual clobber)")
    classify(env, ISLARGE, IDLE, "IsLarge", "State==IDLE")
    print("\n### PAIR 4 — reachable-with-ordering (expect REACHABLE)")
    classify(env, MOTOR, SORTING, "Motor", "State==SORTING")
    print("\n### CONTROL — Motor + hold State==IDLE (expect REACHABLE)")
    classify(env, MOTOR, IDLE, "Motor", "State==IDLE")


if __name__ == "__main__":
    main()
