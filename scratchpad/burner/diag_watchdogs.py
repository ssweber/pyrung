"""Enumerate complement-reset watchdog timers and check input resolution."""
from __future__ import annotations
import os, sys
from pathlib import Path

CLICK_PROJECT = Path(os.environ.get("PYRUNG_CLICK_PROJECT",
    r"C:\Users\Sam\AppData\Local\Temp\CLICK (0009051C)\pyrung_project"))
sys.path.insert(0, str(CLICK_PROJECT))
from main import logic  # noqa: E402
from pyrung import PLC  # noqa: E402
from pyrung.core.analysis.pdg import build_program_graph, _extract_reads_from_condition  # noqa: E402
from pyrung.core.analysis.pilot.trace import trace_back  # noqa: E402
from pyrung.core.instruction.timers import OnDelayInstruction  # noqa: E402
from pyrung.core.validation._common import walk_instructions  # noqa: E402

plc = PLC(logic)
pdg = build_program_graph(logic)
snap = dict(plc.state.tags)
# steerable = input-role tags
from pyrung.core.analysis.pdg import TagRole  # noqa: E402
steerable = frozenset(t for t, r in pdg.tag_roles.items() if r == TagRole.INPUT)
print(f"steerable inputs: {len(steerable)}; x_RotateSensor in steerable: {'x_RotateSensor' in steerable}")
print(f"i_RotateSensor role: {pdg.tag_roles.get('i_RotateSensor')}")

def resolve(tag):
    if tag in steerable:
        return tag, "direct"
    try:
        tree = trace_back(tag, True, snap, pdg, logic, steerable)
    except Exception as e:  # noqa: BLE001
        return None, f"exc:{e}"
    leaves = list(tree.steerable_leaves())
    return (leaves[0][0] if leaves else None), f"{len(leaves)} leaves: {leaves[:3]}"

print("\n--- OnDelay timers with reset_condition ---")
for instr in walk_instructions(logic):
    if not isinstance(instr, OnDelayInstruction) or instr.reset_condition is None:
        continue
    done = instr.done_bit.name
    reads = _extract_reads_from_condition(instr.reset_condition, {})
    print(f"\ntimer done={done} preset={instr.preset!r} unit={instr.unit}")
    print(f"  reset reads: {sorted(reads)}")
    for rt in sorted(reads):
        phys, info = resolve(rt)
        print(f"    {rt} -> {phys}   ({info})")
