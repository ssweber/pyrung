"""SPIKE v2: route-universal (forall) ME clobber test.

The v1 spike's clobber(X->Y) was existential ("some way to establish X clobbers
Y").  For a SOUND prune it must be universal over X's establish routes ("EVERY
way to establish X clobbers Y") — a target with an alternative route that dodges
the clobber is NOT mutually exclusive.

This spike enumerates routes per-producer (forcing each via `writer_locks`) and
computes BOTH:
  - clobber_EXISTS(X->Y): ANY route clobbers Y     (the unsound v1 shape)
  - clobber_ALL(X->Y):    EVERY route clobbers Y    (the sound shape)
ME_exists = EXISTS both directions; ME_all = ALL both directions.

Adversarial fixture (should be REACHABLE): target Z has two routes —
  route alpha: auto-latch while Stage==RUNNING (its path drives Stage off PARKED)
  route beta:  manual latch (touches nothing else)
held sibling Stage==PARKED.  EXISTS wrongly flags ME (route alpha clobbers);
ALL correctly clears it (route beta dodges).

Then re-runs the four conveyor cases + control under ALL to confirm no regression.
"""

import os

os.environ["PYRUNG_DAP_ACTIVE"] = "1"

import math

from pyrung.core import PLC, Bool, Int, Program, Rung, copy, latch, reset, rise
from pyrung.core.analysis.pdg import build_program_graph
from pyrung.core.analysis.pilot.physical import install_harness
from pyrung.core.analysis.pilot import trace as T


# --------------------------------------------------------------------------- #
# env setup (program-agnostic)
# --------------------------------------------------------------------------- #
def build_env(program):
    plc = PLC(program, dt=0.010)
    fork = plc.fork(history_budget=math.inf)
    pdg = build_program_graph(program)
    harness_fb = install_harness(fork)
    ref_consts = T.compute_reference_constants(pdg, program)
    steerable = (
        T.compute_steerable(pdg, fork._known_tags_by_name, program) - harness_fb - ref_consts
    )
    snapshot = dict(fork.state.tags)
    return T._env_for(snapshot, pdg, program, steerable)


def _all_writer_rungs(node, out):
    if node.writer_rung is not None:
        out.add(node.writer_rung)
    for ch in node.children:
        _all_writer_rungs(ch, out)


def _producers(env, tag, val):
    out = set()
    for ri in env.pdg.writers_of.get(tag, frozenset()):
        ro = T.resolve_rung(env.program, env.pdg.rung_nodes[ri])
        if ro is not None and T._can_produce(T._written_value_for_tag(ro, tag), val):
            out.add(ri)
    return out


def _writes_off(env, ri, tag, val):
    if ri not in env.pdg.writers_of.get(tag, frozenset()):
        return None
    ro = T.resolve_rung(env.program, env.pdg.rung_nodes[ri])
    if ro is None:
        return None
    wv = T._written_value_for_tag(ro, tag)
    if T._can_produce(wv, val):
        return None
    if tag in env.pdg.rung_nodes[ri].ote_writes:  # OTE / self-clearing -> transient
        return None
    return wv


def _unwrap(wv):
    return getattr(wv, "value", wv)


def clobber_routes(env, x, y):
    """Per-route clobber analysis of 'establishing X clobbers held Y'.
    Returns list of (producer_ri, [(clobber_ri, written_value), ...]) — one entry
    per establish route of X, with the retentive clobbers of Y on that route."""
    xt, xv = x
    yt, yv = y
    routes = []
    for ri in sorted(_producers(env, xt, xv)):
        tree = T.trace_back(
            xt, xv, env.snapshot, env.pdg, env.program, env.steerable,
            writer_locks={(xt, xv): ri},
        )
        rungs = {ri}
        _all_writer_rungs(tree, rungs)
        hits = []
        for rj in sorted(rungs):
            wv = _writes_off(env, rj, yt, yv)
            if wv is not None:
                hits.append((rj, wv))
        routes.append((ri, hits))
    return routes


def _label(env, ri):
    return T._scope_ref(ri, env.pdg.rung_nodes[ri])


def _fmt_routes(env, routes, ytag):
    parts = []
    for ri, hits in routes:
        if hits:
            h = "; ".join(f"{_label(env, rj)} writes {ytag}={_unwrap(wv)!r}" for rj, wv in hits)
            parts.append(f"[{_label(env, ri)}: CLOBBERS ({h})]")
        else:
            parts.append(f"[{_label(env, ri)}: clean]")
    return " ".join(parts) or "(no producers)"


def classify(env, a, b, name_a, name_b):
    at, av = a
    bt, bv = b
    print("=" * 78)
    print(f"PAIR:  {name_a} ({at}={av!r})  +  {name_b} ({bt}={bv!r})")
    if at == bt and not T._values_match(av, bv):
        print(f"  VERDICT: UNREACHABLE — same register {at}, values {av!r}/{bv!r}")
        return

    ab = clobber_routes(env, a, b)
    ba = clobber_routes(env, b, a)
    print(f"  {name_a}->{name_b} routes: {_fmt_routes(env, ab, bt)}")
    print(f"  {name_b}->{name_a} routes: {_fmt_routes(env, ba, at)}")

    def _exists(routes):
        return any(hits for _, hits in routes)

    def _all(routes):
        return bool(routes) and all(hits for _, hits in routes)

    me_exists = _exists(ab) and _exists(ba)
    me_all = _all(ab) and _all(ba)
    tag = "  ".join([
        f"EXISTS-verdict: {'UNREACHABLE' if me_exists else 'reachable'}",
        f"ALL-verdict: {'UNREACHABLE' if me_all else 'REACHABLE'}",
    ])
    print(f"  {tag}")
    if me_exists != me_all:
        print("  <<< EXISTS and ALL DISAGREE — this is why the sound prune must be ALL >>>")


def _adversarial():
    ManualZ = Bool("ManualZ", external=True)
    EnterRun = Bool("EnterRun", external=True)
    Home = Bool("Home", external=True)
    Stage = Int("Stage")  # 0=PARKED, 1=RUNNING
    Z = Bool("Z")
    with Program() as prog:
        with Rung(Stage == 0, rise(EnterRun)):
            copy(1, Stage)  # PARKED -> RUNNING
        with Rung(Stage == 1):
            latch(Z)  # route alpha: auto-latch in RUNNING (path clobbers Stage==PARKED)
        with Rung(ManualZ):
            latch(Z)  # route beta: manual latch — clean
        with Rung(Home):
            reset(Z)
            copy(0, Stage)  # producer of Stage==PARKED that co-writes reset(Z)
    return prog


def main():
    print("\n########## ADVERSARIAL fixture — Z + hold Stage==PARKED (truly REACHABLE) ##########")
    env = build_env(_adversarial())
    classify(env, ("Z", True), ("Stage", 0), "Z", "Stage==PARKED")

    print("\n\n########## CONVEYOR — regression check under ALL ##########")
    os.environ["PYRUNG_DAP_ACTIVE"] = "1"
    from examples import click_conveyor as cv

    cenv = build_env(cv.logic)
    MOTOR = ("ConveyorMotor", True)
    DIVERTER = ("DiverterCmd", True)
    IDLE = ("State", 0)
    SORTING = ("State", 2)
    ISLARGE = ("IsLarge", True)
    classify(cenv, MOTOR, DIVERTER, "Motor", "Diverter")
    classify(cenv, IDLE, SORTING, "State==IDLE", "State==SORTING")
    classify(cenv, ISLARGE, IDLE, "IsLarge", "State==IDLE")
    classify(cenv, MOTOR, SORTING, "Motor", "State==SORTING")
    classify(cenv, MOTOR, IDLE, "Motor", "State==IDLE")


if __name__ == "__main__":
    main()
