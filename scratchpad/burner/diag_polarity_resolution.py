"""Why does _liveness_hypotheses find only {True} for x_RotateSensor?

Reproduce the rotate ejection incident, then for EACH complement-reset watchdog
print: reset condition, resetting polarity, and whether trace_back can resolve a
steerable leaf for it.  Hypothesis: trace_back short-circuits on the polarity the
sensor is ALREADY at (parked False), so only the currently-unsatisfied polarity
(True) is discoverable from a single incident.
"""
from __future__ import annotations
import os, sys
from pathlib import Path
from types import SimpleNamespace

CLICK = Path(os.environ.get("PYRUNG_CLICK_PROJECT",
    r"C:\Users\ssweb\AppData\Local\Temp\CLICK (00010A00)\pyrung_project"))
sys.path.insert(0, str(CLICK))
from main import logic  # noqa: E402
from pyrung import PLC  # noqa: E402
from pyrung.core.analysis.pdg import build_program_graph, TagRole, _extract_reads_from_condition  # noqa: E402
from pyrung.core.instruction.timers import OnDelayInstruction  # noqa: E402
from pyrung.core.validation._common import walk_instructions  # noqa: E402
from pyrung.core.analysis.pilot.trace import trace_back  # noqa: E402
from pyrung.core.analysis.pilot.investigate import _resetting_polarity, build_deviation_incident  # noqa: E402
from pyrung.core.analysis.pilot._ops import _coast_holding_state  # noqa: E402

pdg = build_program_graph(logic)
steerable = frozenset(t for t, r in pdg.tag_roles.items() if r == TagRole.INPUT)

plc = PLC(logic); plc.step()
plc.patch({"C_ProductionMode": True, "C_UnitModeChgRequest": True}); plc.step(); plc.step()
for k in ("x_BlowerFB", "x_RotateFB", "x_DoorClosed", "x_LintDoorClosed", "x_SailRelay"):
    plc.force(k, True)
plc.force("x_RotateSensor", False)
for name in ("C_Clear", "C_Reset", "C_Start"):
    plc.patch({name: True}); plc.step()
    for _ in range(4): plc.step()
_coast_holding_state(plc, "S_StateCurrent", 6, ("S_StateCurrent",))
anchor = plc.state.scan_id
before = dict(plc.state.tags)
_coast_holding_state(plc, "y_BurnerLoop", True, ("S_StateCurrent", "S_StateRequested"))
after = dict(plc.state.tags)
print(f"Execute@{anchor} -> coast end @{plc.state.scan_id} "
      f"S_StateCurrent={after.get('S_StateCurrent')} Rotate_Error={after.get('Rotate_Error')}")
print(f"i_RotateSensor in after = {after.get('i_RotateSensor')!r}; "
      f"x_RotateSensor = {after.get('x_RotateSensor')!r}")

incident = build_deviation_incident(plc, anchor_scan=anchor, end_scan=plc.state.scan_id,
    action=(), bearing=(("S_StateCurrent", 6),), before_snap=before, after_snap=after)
changed = set(incident.changed_tags)

print("\n--- each complement-reset watchdog ---")
for instr in walk_instructions(logic):
    if not isinstance(instr, OnDelayInstruction) or instr.reset_condition is None:
        continue
    reads = _extract_reads_from_condition(instr.reset_condition, {})
    if "i_RotateSensor" not in reads and "x_RotateSensor" not in reads:
        continue
    rt = next(iter(reads))
    rv = _resetting_polarity(instr.reset_condition, rt, after)
    fired = instr.done_bit.name in changed
    # try trace_back both at the resetting value
    leaves = None
    if rv is not None and rt not in steerable:
        try:
            tree = trace_back(rt, rv, after, pdg, logic, steerable,
                              opaque_loop=frozenset(), pipeline_internal_tags=frozenset(), choice=None)
            leaves = list(tree.steerable_leaves())
        except Exception as e:  # noqa: BLE001
            leaves = f"EXC {e}"
    print(f"{instr.done_bit.name}: reads={reads} resetting_val={rv!r} fired={fired} "
          f"after[{rt}]={after.get(rt)!r}")
    print(f"    trace_back({rt}={rv!r}) leaves = {leaves}")
