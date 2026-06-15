"""Probe: trace why x_RotateSensor doesn't appear in the walker's analysis.

Three questions:
1. Is x_RotateSensor in the PDG upstream cone of y_BurnerLoop / S_CurrStep_Dry?
2. What does why(S_CurrStep_Dry) return from cold?
3. What does why(S_CurrStep_Dry) return from a state where mode is solved
   and the walker would be stuck?
"""

import sys

PROJECT = r"C:\Users\ssweb\AppData\Local\Temp\CLICK (000A0188)\pyrung_project"
sys.path.insert(0, PROJECT)

from main import logic  # noqa: E402
import tags as T  # noqa: E402

from pyrung import PLC  # noqa: E402
from pyrung.core.analysis.pdg import build_program_graph  # noqa: E402


def dump_chain(label, chain):
    print(f"\n{'='*60}")
    print(f"  {label}")
    print(f"{'='*60}")
    if chain is None:
        print("  (None)")
        return
    print(f"  mode={chain.mode}")
    for i, step in enumerate(chain.steps):
        trigs = [(t.tag_name, t.to_value) for t in step.triggers]
        enabs = [(e.tag_name, e.held_value) for e in step.enablers]
        print(f"  step {i}: rung={step.rung_index} sub={step.subroutine}")
        if trigs:
            print(f"    triggers: {trigs}")
        if enabs:
            print(f"    enablers: {enabs}")
    roots = [(r.tag_name, r.to_value) for r in chain.conjunctive_roots]
    print(f"  conjunctive_roots: {roots}")
    if chain.ambiguous_roots:
        amb = [(r.tag_name, r.to_value) for r in chain.ambiguous_roots]
        print(f"  ambiguous_roots: {amb}")


# --- 1. PDG upstream cones ---
print("Building PDG...", flush=True)
plc = PLC(logic)
plc.step()
pdg = build_program_graph(plc._program)

for target in ("y_BurnerLoop", "S_CurrStep_Dry"):
    cone = pdg.upstream_slice(target)
    cone_all = pdg.upstream_slice_all(target)
    cone_calls = pdg.upstream_slice_with_calls(target)
    rotate_in = "x_RotateSensor" in cone
    rotate_in_all = "x_RotateSensor" in cone_all
    rotate_in_calls = "x_RotateSensor" in cone_calls

    print(f"\nPDG upstream of {target}:")
    print(f"  cone size: {len(cone)} / all: {len(cone_all)} / with_calls: {len(cone_calls)}")
    print(f"  x_RotateSensor in cone:      {rotate_in}")
    print(f"  x_RotateSensor in cone_all:  {rotate_in_all}")
    print(f"  x_RotateSensor in cone_calls:{rotate_in_calls}")

    # Show any alarm/rotate/watchdog tags in the cone
    rotate_related = sorted(t for t in cone_calls if "otate" in t or "Alm11" in t or "atchdog" in t)
    if rotate_related:
        print(f"  rotate-related tags in cone: {rotate_related}")
    else:
        print(f"  NO rotate-related tags found in upstream cone")

# --- 2. why() from cold ---
print("\n\n--- why() from COLD (state=9, mode=3) ---")
dump_chain("why(S_CurrStep_Dry) from cold", plc.why("S_CurrStep_Dry"))
dump_chain("why(y_BurnerLoop) from cold", plc.why("y_BurnerLoop"))

# --- 3. why() from walker-like state (mode solved, starting) ---
print("\n\n--- Reproduce walker's stuck state ---")
plc2 = PLC(logic)
plc2.step()
# Solve mode (what the walker does)
plc2.patch({"C_ProductionMode": True, "C_UnitModeChgRequest": True})
plc2.step()
plc2.step()
print(f"mode={plc2.state.tags['S_UnitModeCurrent']}", flush=True)
# Clear + Reset + Start (what the walker does next)
plc2.patch({"C_Clear": True})
plc2.step()
plc2.step()
plc2.patch({"C_Reset": True})
plc2.step()
plc2.step()
print(f"state={plc2.state.tags['S_StateCurrent']}", flush=True)
plc2.patch({"C_Start": True})
plc2.step()
print(f"state={plc2.state.tags['S_StateCurrent']} (should be Starting=3)", flush=True)

# Run 1300 scans (13s sim) WITHOUT toggling x_RotateSensor — watchdog should fire
for i in range(1300):
    plc2.step()
state_after = plc2.state.tags["S_StateCurrent"]
mode_after = plc2.state.tags["S_UnitModeCurrent"]
rotate_err = plc2.state.tags.get("Rotate_Error", "?")
alm11 = plc2.state.tags.get("A_Alm11_Rotate_Trig", "?")
print(f"after 1300 scans (no toggle): state={state_after} mode={mode_after} "
      f"Rotate_Error={rotate_err} A_Alm11_Rotate_Trig={alm11}", flush=True)

dump_chain("why(S_CurrStep_Dry) after watchdog", plc2.why("S_CurrStep_Dry"))
dump_chain("why(S_StateCurrent) after watchdog", plc2.why("S_StateCurrent"))

# Also try cause() on the state transition if there's history
print("\n--- cause() on state transition ---")
try:
    chain = plc2.cause("S_StateCurrent")
    dump_chain("cause(S_StateCurrent) most recent", chain)
except Exception as e:
    print(f"cause(S_StateCurrent) raised: {e}")

try:
    chain = plc2.cause("S_UnitModeCurrent")
    dump_chain("cause(S_UnitModeCurrent) most recent", chain)
except Exception as e:
    print(f"cause(S_UnitModeCurrent) raised: {e}")
