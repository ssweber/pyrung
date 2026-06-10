"""D2 probe: noise-augmented cross-guard program on the CURRENT walker.

Expectation (before generalization): the free-running parity counter
(Cycle) lands in every cause()-named blocking set, exact is_blocked never
fires, the seen-key projection fragments on the live Cycle value, and the
walk fails with recovery_iters == _MAX_RECHECK_ITERS.
"""

from __future__ import annotations

import logging
import sys

sys.path.insert(0, "src")

from pyrung import And, Bool, Int, Or, Program, Rung, Timer, calc, on_delay, out
from pyrung.core.analysis.pdg import build_program_graph
from pyrung.core.analysis.walk import engine as walk
from pyrung.core.runner import PLC

logging.basicConfig(level=logging.INFO, format="%(name)s %(message)s")


def _program() -> tuple[Program, Bool]:
    Input_A = Bool("Input_A", external=True)
    Input_B = Bool("Input_B", external=True)
    Reset_Cmd = Bool("Reset_Cmd", external=True)
    Latch_A = Bool("Latch_A")
    Latch_B = Bool("Latch_B")
    Guard_A = Bool("Guard_A")
    Guard_B = Bool("Guard_B")
    TimerA = Timer.clone("TimerA")
    TimerB = Timer.clone("TimerB")
    Cycle = Int("Cycle")
    Target = Bool("Target")

    with Program() as prog:
        # Free-running per-scan counter (mod-2 parity).
        with Rung():
            calc((Cycle + 1) % 2, Cycle)
        # 30 ms = 3 scans at dt=0.010: dwells stay inside _PULSE_REACT_CAP so
        # corridors complete without a fold jump (the per-scan Cycle churn
        # means no plateau ever forms — folding is unavailable here).
        with Rung(Input_A):
            on_delay(TimerA, 30, "ms")
        with Rung(Input_B):
            on_delay(TimerB, 30, "ms")
        # Latch A: timer-gated arm while ~Guard_B; self-seals.
        with Rung(Or(And(TimerA.Done, ~Guard_B), Latch_A)):
            out(Latch_A)
        # Latch B: timer-gated arm while ~Guard_A AND parity even; self-seals.
        with Rung(Or(And(TimerB.Done, ~Guard_A, Cycle == 0), Latch_B)):
            out(Latch_B)
        with Rung(Or(TimerA.Done, Guard_A), ~Reset_Cmd):
            out(Guard_A)
        with Rung(Or(TimerB.Done, Guard_B), ~Reset_Cmd):
            out(Guard_B)
        with Rung(Latch_A, Latch_B):
            out(Target)

    return prog, Target


def premise() -> None:
    prog, _t = _program()
    plc = PLC(prog, dt=0.010)
    plc.patch({"Input_A": True})
    for _ in range(15):
        plc.step()
    plc.patch({"Input_A": False})
    plc.step()
    assert plc.state.tags["Latch_A"] is True, plc.state.tags
    assert plc.state.tags["Guard_A"] is True
    plc.patch({"Reset_Cmd": True})
    plc.step()
    plc.patch({"Reset_Cmd": False})
    plc.step()
    assert plc.state.tags["Guard_A"] is False
    assert plc.state.tags["Latch_A"] is True
    plc.patch({"Input_B": True})
    for _ in range(15):
        plc.step()
    assert plc.state.tags["Latch_B"] is True, plc.state.tags
    assert plc.state.tags["Target"] is True
    print("PREMISE OK: manual sequence reaches Target")


def walk_probe() -> None:
    prog, target = _program()
    plc = PLC(prog, dt=0.010)

    work = plc.fork()
    walk._install_walk_harness(work)
    pdg = build_program_graph(work._program)
    known = work._known_tags_by_name
    ext_inputs = walk._external_bool_inputs(pdg, known)
    edge_ext = walk._edge_tags(pdg, work._program) & set(ext_inputs)
    governing, gov_value = walk._governing(target.name, True, pdg, work._program, plc=work)
    print(f"governing: {governing} -> {gov_value}")

    store = walk.NoGoodStore()
    holds = walk.HoldStore()
    steps = walk._walk_to_goal(
        work,
        governing,
        gov_value,
        pdg,
        work._program,
        known,
        ext_inputs,
        edge_ext,
        64,
        nogoods=store,
        holds=holds,
    )
    print(f"steps: {steps if steps is None else len(steps)}")
    print(f"recovery_iters: {store.recovery_iters}")
    print(f"nogood count: {len(store._nogoods)}")
    for ng in sorted(store._nogoods, key=str):
        print(f"  nogood: {ng.from_value} -> {ng.to_value} blocking={sorted(ng.blocking)}")
    print(f"blocking names: {sorted(store.blocking_tag_names())}")

    print("\n--- how() verdict ---")
    plc2 = PLC(prog, dt=0.010)
    path = plc2.how(target)
    print(f"how(Target): reachable={path.reachable}")


if __name__ == "__main__":
    premise()
    walk_probe()
