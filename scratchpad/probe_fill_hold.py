"""Trace the fill sequencer hold/divest failure without editing walker code."""

from __future__ import annotations

import logging
import sys
from dataclasses import replace as _replace
from typing import Any

PROJECT = r"C:\Users\Sam\AppData\Local\Temp\CLICK (0175103C)\pyrung_project"
sys.path.insert(0, PROJECT)

logging.basicConfig(level=logging.DEBUG, format="%(name)s %(message)s")
for noisy in ("pyrung.core.analysis.prove", "pyrung.core.runner"):
    logging.getLogger(noisy).setLevel(logging.WARNING)

from main import logic  # noqa: E402
from tags import HMI_fill, c_subStatusOneShot, fill_solv_nc, fill_stepNumber, t_fillDelay  # noqa: E402

from pyrung import PLC  # noqa: E402
from pyrung.circuitpy.codegen import compile_kernel  # noqa: E402
from pyrung.core.analysis.pdg import build_program_graph  # noqa: E402
from pyrung.core.analysis.prove import _build_explore_context, _compile_property  # noqa: E402
from pyrung.core.analysis.prove.passes import _OptConfig  # noqa: E402
from pyrung.core.analysis.walk.agenda import _walk_to_goal  # noqa: E402
from pyrung.core.analysis.walk.base import HoldStore, NoGoodStore  # noqa: E402
from pyrung.core.analysis.walk.passes import run_walk_passes  # noqa: E402
from pyrung.core.analysis.walk.physical import _install_walk_harness  # noqa: E402
from pyrung.core.analysis.walk.priors import _edge_tags, _external_bool_inputs  # noqa: E402


WATCH = (
    "fill_stepNumber",
    "fill_subStatus",
    "c_subStatusOneShot",
    "HMI_on",
    "HMI_resetError",
    "HMI_tare",
    "HMI_fill",
    "sub_fillOff",
    "sub_fillOn",
    "sub_fillFilling",
    "pv_LevelHt",
    "sv_levelSetPoint",
    "calc_levelSvLowerWBand",
    "t_fillDelay_Done",
    "_fill",
    "fill_solv_nc",
    "alarm",
    "msg_error",
)


def show(label: str, plc: PLC) -> None:
    tags = plc.state.tags
    bits = ", ".join(f"{name}={tags.get(name)!r}" for name in WATCH)
    print(f"\n[{label}]\n  {bits}")


def show_holds(holds: HoldStore) -> None:
    if not len(holds):
        print("  holds: <empty>")
        return
    for h in sorted(holds, key=lambda item: item.name):
        print(f"  hold: {h.name}={h.value!r} for {h.goal}")


def print_steps(label: str, steps: list[tuple[dict[str, Any], int]] | None) -> None:
    print(f"\n{label}: {None if steps is None else len(steps)} action(s)")
    if steps is None:
        return
    for i, (action, scans) in enumerate(steps, 1):
        print(f"  {i}. action={action or '{}'} scans={scans}")


def replay(label: str, origin: PLC, steps: list[tuple[dict[str, Any], int]] | None) -> PLC:
    f = origin.fork()
    if steps is None:
        return f
    print(f"\nReplay {label}:")
    show("start", f)
    for i, (action, scans) in enumerate(steps, 1):
        if action:
            f.patch(action)
        for _ in range(scans):
            f.step()
        show(f"after {i}: {action or '{}'} x{scans}", f)
    return f


plc = PLC(logic)
plc.step()
show("cold after one scan", plc)

_, auto_scope, expr = _compile_property(fill_solv_nc, ~HMI_fill)
compiled = compile_kernel(plc._program, blockless=True, proof_metadata=True)
ctx = _build_explore_context(
    plc._program,
    scope=auto_scope,
    extra_exprs=[expr] if expr is not None else [],
    _opt_config=_replace(_OptConfig(), walk_only=True),
    compiled=compiled,
    initial_state=dict(plc.state.tags),
    allow_partial=True,
)
pdg = build_program_graph(plc._program)
advice, _journal = run_walk_passes(plc._program, pdg)
ext_inputs = _external_bool_inputs(pdg, plc._known_tags_by_name, plc._program, advice=advice)
edge_ext = _edge_tags(pdg, plc._program) & set(ext_inputs)
nd = dict(getattr(ctx, "nondeterministic_dims", {}) or {})

work = plc.fork()
_install_walk_harness(work)
holds = HoldStore()
nogoods = NoGoodStore()

for goal in (("fill_stepNumber", 4), ("fill_stepNumber", 5)):
    trial = plc.fork()
    _install_walk_harness(trial)
    local_holds = HoldStore()
    local_nogoods = NoGoodStore()
    print(f"\n=== direct _walk_to_goal {goal} from cold ===")
    steps = _walk_to_goal(
        trial,
        goal[0],
        goal[1],
        pdg,
        plc._program,
        plc._known_tags_by_name,
        ext_inputs,
        edge_ext,
        80,
        nd_domains=nd,
        explore_context=ctx,
        nogoods=local_nogoods,
        holds=local_holds,
        wall_budget_s=60,
    )
    print_steps(str(goal), steps)
    show("mutated work after call", trial)
    show_holds(local_holds)
    print(f"  nogoods: {local_nogoods.entries()}")
    replay(str(goal), plc, steps)

print("\n=== direct fill_stepNumber=3, then c_subStatusOneShot=True ===")
seq = plc.fork()
_install_walk_harness(seq)
seq_holds = HoldStore()
seq_nogoods = NoGoodStore()
steps3 = _walk_to_goal(
    seq,
    "fill_stepNumber",
    3,
    pdg,
    plc._program,
    plc._known_tags_by_name,
    ext_inputs,
    edge_ext,
    80,
    nd_domains=nd,
    explore_context=ctx,
    nogoods=seq_nogoods,
    holds=seq_holds,
    wall_budget_s=60,
)
print_steps("fill_stepNumber=3", steps3)
show("after fill_stepNumber=3", seq)
show_holds(seq_holds)
steps_os_from_3 = _walk_to_goal(
    seq,
    "c_subStatusOneShot",
    True,
    pdg,
    plc._program,
    plc._known_tags_by_name,
    ext_inputs,
    edge_ext,
    80,
    nd_domains=nd,
    explore_context=ctx,
    nogoods=seq_nogoods,
    holds=seq_holds,
    wall_budget_s=60,
)
print_steps("c_subStatusOneShot from step 3", steps_os_from_3)
show("after one-shot-from-3 call", seq)
show_holds(seq_holds)
print(f"  nogoods: {seq_nogoods.entries()}")
if steps3 is not None:
    replay_base = replay("fill_stepNumber=3", plc, steps3)
    replay("c_subStatusOneShot from replayed step 3", replay_base, steps_os_from_3)

    delay_trial = replay_base.fork()
    delay_holds = HoldStore()
    delay_nogoods = NoGoodStore()
    print("\n=== direct t_fillDelay.Done=True from replayed step 3 ===")
    delay_steps = _walk_to_goal(
        delay_trial,
        t_fillDelay.Done.name,
        True,
        pdg,
        plc._program,
        plc._known_tags_by_name,
        ext_inputs,
        edge_ext,
        80,
        nd_domains=nd,
        explore_context=ctx,
        nogoods=delay_nogoods,
        holds=delay_holds,
        wall_budget_s=60,
    )
    print_steps("t_fillDelay.Done from step 3", delay_steps)
    show("after delay call", delay_trial)
    show_holds(delay_holds)
    print(f"  nogoods: {delay_nogoods.entries()}")
    replay("t_fillDelay.Done from replayed step 3", replay_base, delay_steps)

print("\n=== direct c_subStatusOneShot=True from cold ===")
steps = _walk_to_goal(
    work,
    "c_subStatusOneShot",
    True,
    pdg,
    plc._program,
    plc._known_tags_by_name,
    ext_inputs,
    edge_ext,
    80,
    nd_domains=nd,
    explore_context=ctx,
    nogoods=nogoods,
    holds=holds,
    wall_budget_s=60,
)
print_steps("c_subStatusOneShot", steps)
show("mutated work after one-shot call", work)
show_holds(holds)
print(f"  nogoods: {nogoods.entries()}")
replay("c_subStatusOneShot", plc, steps)
