"""Instrument the existing cross-guard tripwire: where does recovery fire,
what does cause() name per round, and why don't holds prevent the clobber?"""

from __future__ import annotations

import logging
import sys

sys.path.insert(0, "src")
sys.path.insert(0, ".")

from tests.core.analysis.test_walk_nogood import _program

from pyrung.core.analysis.pdg import build_program_graph
from pyrung.core.analysis.walk import engine as walk
from pyrung.core.runner import PLC

logging.basicConfig(level=logging.INFO, format="%(name)s %(message)s")

# Patch _recheck_prereqs to print what cause names each round.
import pyrung.core.analysis.walk.agenda as agenda

_orig_recheck = agenda._recheck_prereqs


def _spy_recheck(work, target_tag, target_value):
    goals = _orig_recheck(work, target_tag, target_value)
    print(f"  RECHECK {target_tag}->{target_value}: goals={goals}")
    print(f"    state: {dict((k, v) for k, v in work.state.tags.items() if not k.startswith('_'))}")
    return goals


agenda._recheck_prereqs = _spy_recheck


def main() -> None:
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
    for ng in sorted(store._nogoods, key=str):
        print(f"  nogood: {ng.from_value} -> {ng.to_value} blocking={sorted(ng.blocking)}")
    print(f"holds: {sorted(holds.protected().items())}")


if __name__ == "__main__":
    main()
