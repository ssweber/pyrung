"""Ground truth: is (fill_solv_nc=True, HMI_fill=False) concretely reachable?

Dance: hold HMI_on; raise sv_levelHtMax (ND, never written) so pv=100
doesn't trip the max alarm; tare while pv=100 (setpoint := 100); then
drop the level reading (systemLevel=100 -> pv=0) so the fill-delay gate
pv < lower(100) passes; sequencer 1->2->3->4->5; filling holds (_fill on,
pv < upper so never "full"); io: fill_solv_nc = (_fill | HMI_fill) & ~alarm.
"""

import sys

PROJECT = r"C:\Users\Sam\AppData\Local\Temp\CLICK (0175103C)\pyrung_project"
sys.path.insert(0, PROJECT)

from main import logic  # noqa: E402
from tags import (  # noqa: E402
    HMI_on,
    HMI_tare,
    sv_levelHtMax,
    systemLevel_opt2011,
)

from pyrung import PLC  # noqa: E402

plc = PLC(logic)
plc.step()

f = plc.fork()
f.patch({HMI_on: True, sv_levelHtMax: 100.0, HMI_tare: True})
f.step()  # tare: setpoint := pv (=100)
f.patch({HMI_tare: False, systemLevel_opt2011: 100.0})  # pv -> 0
for i in range(30):
    f.step()
    t = f.state.tags
    print(
        f"scan {i}: step={t.get('fill_stepNumber')} sp={t.get('sv_levelSetPoint')}"
        f" pv={t.get('pv_LevelHt')} msg_error={t.get('msg_error')}"
        f" alarm={t.get('alarm')} _fill={t.get('_fill')}"
        f" HMI_fill={t.get('HMI_fill')} fill_solv_nc={t.get('fill_solv_nc')}"
    )
    if t.get("fill_solv_nc") and not t.get("HMI_fill"):
        print(f"\nGROUND TRUTH: fill_solv_nc=True with HMI_fill=False at scan {i}")
        break
else:
    print("\nGROUND TRUTH: NOT reached in 30 scans")
