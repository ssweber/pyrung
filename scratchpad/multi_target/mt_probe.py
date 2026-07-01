"""Pressure-test multi-target design against pilot's real trace on click_conveyor.

Drives trace_back on target pairs and prints the prerequisite trees + steerable
leaves, so we can see (a) shared prereqs, (b) whether preserve cross-applies,
(c) the mutual-exclusion signal.
"""

import os

os.environ["PYRUNG_DAP_ACTIVE"] = "1"  # suppress the example's import-time sim

import math

from examples import click_conveyor as cv
from pyrung.core.runner import PLC
from pyrung.core.analysis.pdg import build_program_graph
from pyrung.core.analysis.pilot.physical import install_harness
from pyrung.core.analysis.pilot.trace import (
    trace_back,
    compute_steerable,
    compute_reference_constants,
)


def _setup():
    plc = PLC(cv.logic, dt=0.010)
    program = plc._program
    fork = plc.fork(history_budget=math.inf)
    pdg = build_program_graph(program)
    harness_fb = install_harness(fork)
    ref_consts = compute_reference_constants(pdg, program)
    steerable = compute_steerable(pdg, fork._known_tags_by_name, program) - harness_fb - ref_consts
    snapshot = dict(fork.state.tags)
    return fork, program, pdg, steerable, snapshot


def render(node, depth=0):
    ind = "  " * depth
    flags = []
    if node.satisfied:
        flags.append("SAT")
    if node.is_steerable:
        flags.append("STEER")
    if node.self_advancing:
        flags.append("COAST")
    if node.oscillate:
        flags.append("OSC")
    if node.data_flow:
        flags.append(node.data_flow)
    if node.provenance:
        flags.append("prov=" + ",".join(node.provenance))
    tag = f"{node.tag}={node.value!r}"
    print(f"{ind}{tag}  [{' '.join(flags)}]")
    for ch in node.children:
        render(ch, depth + 1)


def trace_target(label, tag, value, fork, program, pdg, steerable, snapshot):
    print("=" * 72)
    print(f"TARGET {label}:  {tag} == {value!r}")
    print("-" * 72)
    node = trace_back(tag, value, snapshot, pdg, program, steerable)
    render(node)
    leaves = node.steerable_leaves()
    print(f"\n  steerable leaves: {sorted(set(leaves))}")
    return set(leaves)


def main():
    fork, program, pdg, steerable, snapshot = _setup()
    print("steerable tags:", sorted(steerable))
    print("snapshot (non-false):", {k: v for k, v in snapshot.items() if v not in (False, 0, None)})

    args = (fork, program, pdg, steerable, snapshot)

    print("\n\n########## PAIR 1: two outputs — Motor + Diverter ##########")
    motor = trace_target("ConveyorMotor", "ConveyorMotor", True, *args)
    div = trace_target("DiverterCmd", "DiverterCmd", True, *args)
    print("\nSHARED steerable leaves:", sorted(motor & div))
    print("Motor-only:", sorted(motor - div))
    print("Diverter-only:", sorted(div - motor))

    print("\n\n########## PAIR 2: mutually-exclusive State values ##########")
    idle = trace_target("State==IDLE", "State", 0, *args)
    sorting = trace_target("State==SORTING", "State", 2, *args)
    print("\nSHARED steerable leaves:", sorted(idle & sorting))

    print("\n\n########## PAIR 3: cross-tag ME — IsLarge + State==IDLE ##########")
    islarge = trace_target("IsLarge", "IsLarge", True, *args)
    idle2 = trace_target("State==IDLE", "State", 0, *args)
    print("\nSHARED:", sorted(islarge & idle2))

    print("\n\n########## PAIR 4: reachable-with-ordering — Motor + State==SORTING ##########")
    motor2 = trace_target("ConveyorMotor", "ConveyorMotor", True, *args)
    sorting2 = trace_target("State==SORTING", "State", 2, *args)
    print("\nSHARED:", sorted(motor2 & sorting2))
    print("Union:", sorted(motor2 | sorting2))


if __name__ == "__main__":
    main()
