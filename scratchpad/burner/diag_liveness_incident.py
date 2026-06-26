"""Reproduce the terminal-letrun ejection and run _liveness_hypotheses on it."""
from __future__ import annotations
import os, sys
from pathlib import Path

CLICK_PROJECT = Path(os.environ.get("PYRUNG_CLICK_PROJECT",
    r"C:\Users\Sam\AppData\Local\Temp\CLICK (0009051C)\pyrung_project"))
sys.path.insert(0, str(CLICK_PROJECT))
from main import logic  # noqa: E402
from pyrung import PLC  # noqa: E402
from pyrung.core.analysis.pdg import build_program_graph, TagRole  # noqa: E402
from pyrung.core.analysis.pilot.investigate import _liveness_hypotheses, build_deviation_incident  # noqa: E402
from pyrung.core.analysis.pilot._ops import _coast_holding_state  # noqa: E402

pdg = build_program_graph(logic)
steerable = frozenset(t for t, r in pdg.tag_roles.items() if r == TagRole.INPUT)

class Ctx:
    pass
ctx = Ctx()
ctx.pdg = pdg
ctx.program = logic
ctx.steerable = steerable
ctx.opaque_loop = frozenset()
ctx.pipeline_internal_tags = frozenset()
ctx.choice = None

def pulse(plc, name, settle=4):
    plc.patch({name: True}); plc.step()
    for _ in range(settle): plc.step()

plc = PLC(logic); plc.step()
plc.patch({"C_ProductionMode": True, "C_UnitModeChgRequest": True}); plc.step(); plc.step()
pulse(plc, "C_Clear"); pulse(plc, "C_Reset")
for k in ("x_BlowerFB","x_RotateFB","x_DoorClosed","x_LintDoorClosed"):
    plc.force(k, True)
pulse(plc, "C_Start")
# leg 1: coast to Execute
_coast_holding_state(plc, "S_StateCurrent", 6, ("S_StateCurrent",))
anchor = plc.state.scan_id
before = dict(plc.state.tags)
print(f"at Execute scan={anchor} S_StateCurrent={before.get('S_StateCurrent')}")
# leg 2: terminal let-run toward y_BurnerLoop holding macro-state -> ejects
_coast_holding_state(plc, "y_BurnerLoop", True, ("S_StateCurrent","S_StateRequested"))
end = plc.state.scan_id
after = dict(plc.state.tags)
print(f"after coast scan={end} S_StateCurrent={after.get('S_StateCurrent')} Rotate_Error={after.get('Rotate_Error')}")

incident = build_deviation_incident(
    plc, anchor_scan=anchor, end_scan=end, action=(),
    bearing=(("S_StateCurrent", 6),), before_snap=before, after_snap=after,
)
wd = [t for t in incident.changed_tags if "WD" in t or "SensorO" in t]
print(f"\nchanged_tags count={len(incident.changed_tags)}")
print(f"WD-ish in changed_tags: {wd}")
print(f"Rotate_SensorOnWD_tmr_Done in changed: {'Rotate_SensorOnWD_tmr_Done' in incident.changed_tags}")
print(f"Rotate_SensorOffWD_tmr_Done in changed: {'Rotate_SensorOffWD_tmr_Done' in incident.changed_tags}")

hyps = _liveness_hypotheses(plc, incident, ctx)
print(f"\nliveness hypotheses: {len(hyps)}")
for h in hyps:
    print(f"  [{h.kind}] {h.holds}  -- {h.detail}")
