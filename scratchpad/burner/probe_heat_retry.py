"""Focused dump around heat's first scan: why doesn't the retry fire?"""

import sys

PROJECT = r"C:\Users\Sam\AppData\Local\Temp\CLICK (0032023C)\pyrung_project"
sys.path.insert(0, PROJECT)

from main import logic  # noqa: E402
import tags as T  # noqa: E402

from pyrung import PLC  # noqa: E402

plc = PLC(logic)
plc.step()
plc.patch({T.S_StateCurrent: 9})
plc.step()
for xt in (T.x_DoorClosed, T.x_LintDoorClosed, T.x_SailRelay, T.x_RotateFB, T.x_BlowerFB):
    plc.force(xt, True)
plc.step()


def v(name):
    return plc.current_state.tags.get(name, "<missing>")


def go(pulse=None, until=None, max_scans=3000, toggle=False):
    n = 0
    if pulse:
        plc.patch({getattr(T, k): True for k in pulse})
    while n < max_scans:
        if toggle:
            plc.force(T.x_RotateSensor, (n // 50) % 2 == 0)
        plc.step()
        n += 1
        if until and until():
            return n
    return n


go(pulse=["C_ProductionMode", "C_UnitModeChgRequest"], until=lambda: v("S_UnitModeCurrent") == 1, max_scans=20)
go(pulse=["C_Clear"], until=lambda: v("S_StateCurrent") == 2, max_scans=200)
go(pulse=["C_Reset"], until=lambda: v("S_StateCurrent") == 4, max_scans=200)
go(pulse=["C_Start"], until=lambda: v("S_StateCurrent") == 6, max_scans=3000, toggle=True)
go(until=lambda: v("Heat_xCall") == 1, max_scans=2000, toggle=True)

COLS = ["Heat_CurStep", "Heat_Error", "Heat_Limit_Ts", "Heat_xReset",
        "S_CurrHeatRetryCount", "S_P6_HeatMaxRetry", "C_P6_HeatMaxRetry",
        "Heat_EnableLimit", "Heat_xCall", "S_StateCurrent"]
print("scan-rel | " + " | ".join(COLS), flush=True)
print(f"      +0 | " + " | ".join(str(v(c)) for c in COLS), flush=True)
for i in range(1, 8):
    go(max_scans=1, toggle=True)
    print(f"      +{i} | " + " | ".join(str(v(c)) for c in COLS), flush=True)
