"""D2 tripwire candidate v2: shared-cone cross-guard + mod-3 noise conjunct.

Knobs:
  --no-noise   build the noise-free twin (must solve today)
"""

from __future__ import annotations

import logging
import sys

sys.path.insert(0, "src")
sys.path.insert(0, ".")

from pyrung import And, Bool, Int, Or, Program, Rung, Timer, calc, on_delay, out
from pyrung.core.analysis.pdg import build_program_graph
from pyrung.core.analysis.walk import engine as walk
from pyrung.core.runner import PLC

logging.basicConfig(level=logging.INFO, format="%(name)s %(message)s")

import pyrung.core.analysis.walk.agenda as agenda

_orig_recheck = agenda._recheck_prereqs


def _spy_recheck(work, target_tag, target_value):
    try:
        chain = work.cause(target_tag, to=target_value)
        mode = getattr(chain, "mode", "?") if chain is not None else "none"
    except Exception as e:  # noqa: BLE001
        mode = f"err:{e}"
    goals = _orig_recheck(work, target_tag, target_value)
    tags = work.state.tags
    brief = {
        k: tags.get(k)
        for k in ("Guard_A", "Guard_B", "TimerB_Done", "TimerA_Done", "Cycle",
                  "Latch_A", "Latch_B", "Input_A", "Input_B", "Reset_Cmd")
        if k in tags
    }
    print(f"  RECHECK {target_tag}->{target_value} mode={mode}: goals={goals}")
    print(f"    brief: {brief}")
    return goals


agenda._recheck_prereqs = _spy_recheck


def _program(noise: bool) -> tuple[Program, Bool]:
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

    if noise:
        with Program() as prog:
            with Rung():
                calc((Cycle + 1) % 3, Cycle)
            with Rung(Input_A):
                on_delay(TimerA, 30, "ms")
            # (A ~Reset_Cmd conjunct here — shared cone with Guard_A — was
            # probed and discarded: it makes even the noise-free control
            # unsolvable for today's walker.)
            with Rung(Input_B):
                on_delay(TimerB, 30, "ms")
            with Rung(Or(And(TimerA.Done, ~Guard_B), Latch_A)):
                out(Latch_A)
            with Rung(Or(And(TimerB.Done, ~Guard_A, Cycle == 2), Latch_B)):
                out(Latch_B)
            with Rung(Or(TimerA.Done, Guard_A), ~Reset_Cmd):
                out(Guard_A)
            with Rung(Or(TimerB.Done, Guard_B), ~Reset_Cmd):
                out(Guard_B)
            with Rung(Latch_A, Latch_B):
                out(Target)
    else:
        with Program() as prog:
            with Rung(Input_A):
                on_delay(TimerA, 30, "ms")
            with Rung(Input_B):
                on_delay(TimerB, 30, "ms")
            with Rung(Or(And(TimerA.Done, ~Guard_B), Latch_A)):
                out(Latch_A)
            with Rung(Or(And(TimerB.Done, ~Guard_A), Latch_B)):
                out(Latch_B)
            with Rung(Or(TimerA.Done, Guard_A), ~Reset_Cmd):
                out(Guard_A)
            with Rung(Or(TimerB.Done, Guard_B), ~Reset_Cmd):
                out(Guard_B)
            with Rung(Latch_A, Latch_B):
                out(Target)

    return prog, Target


def premise(noise: bool) -> None:
    prog, _t = _program(noise)
    plc = PLC(prog, dt=0.010)
    plc.patch({"Input_A": True})
    for _ in range(8):
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
    for _ in range(10):
        plc.step()
    assert plc.state.tags["Latch_B"] is True, plc.state.tags
    assert plc.state.tags["Target"] is True
    print(f"PREMISE OK (noise={noise})")


def walk_probe(noise: bool) -> None:
    prog, target = _program(noise)
    plc = PLC(prog, dt=0.010)

    work = plc.fork()
    walk._install_walk_harness(work)
    pdg = build_program_graph(work._program)
    known = work._known_tags_by_name
    ext_inputs = walk._external_bool_inputs(pdg, known)
    edge_ext = walk._edge_tags(pdg, work._program) & set(ext_inputs)
    governing, gov_value = walk._governing(target.name, True, pdg, work._program, plc=work)

    store = walk.NoGoodStore()
    holds = walk.HoldStore() if "--holds" in sys.argv else None
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
    print(f"== noise={noise} holds={holds is not None}: "
          f"steps={steps if steps is None else len(steps)} "
          f"iters={store.recovery_iters} nogoods={len(store._nogoods)}")
    for ng in sorted(store._nogoods, key=str):
        print(f"   nogood: {ng.from_value}->{ng.to_value} {sorted(ng.blocking)}")

    plc2 = PLC(prog, dt=0.010)
    path = plc2.how(target)
    print(f"== noise={noise}: how(Target) reachable={path.reachable}")


if __name__ == "__main__":
    noise = "--no-noise" not in sys.argv
    premise(noise)
    walk_probe(noise)
