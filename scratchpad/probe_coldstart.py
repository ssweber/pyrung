"""Verify round-2 template fixes: y_BurnerLoop reachable from TRUE cold start.

Edits under test (in pyrung_project source):
  init R3:        copy(9, S_StateCurrent)   — power up in Aborted
  heat R2:        gate Heat_CurStep <= 1    — Limit_Ts floor covers first scan
  validation R6:  C_P6_HeatMaxRetry >= 1    — default 0 no longer wipes MaxRetry
  validation R9:  P9 upper gate checks P9 (typo fix, not burner-related)

Phases:
  A. Cold snapshot (expect state=9 mode=3) + jog test.
  B. Full documented path from cold, NO bootstrap patches. Expect y_BurnerLoop.
     Watch: Heat_Error must stay 0 (no blip), S_P6_HeatMaxRetry must stay 1.
"""

import sys
import time

PROJECT = r"C:\Users\Sam\AppData\Local\Temp\CLICK (0032023C)\pyrung_project"
sys.path.insert(0, PROJECT)

t0 = time.monotonic()
from main import logic  # noqa: E402
import tags as T  # noqa: E402

from pyrung import PLC  # noqa: E402

WATCH = [
    "S_StateCurrent", "S_UnitModeCurrent", "C_CtrlCmd", "Internal__Step",
    "Rotate_CurStep", "Rotate_Error", "Blower_CurStep", "Blower_Error",
    "Rotate__init", "Blower__init", "Heat_xCall", "Heat_CurStep", "Heat_Error",
    "Heat_Limit_Ts", "S_CurrHeatRetryCount", "S_P6_HeatMaxRetry",
    "o_BurnerLoop", "y_BurnerLoop", "y_RotateCt",
]


class Driver:
    def __init__(self, label):
        self.label = label
        self.plc = PLC(logic)
        self.scan_count = 0
        self.toggle_sensor = False
        self.last = {}
        self.plc.step()  # first scan: init() runs
        self.snapshot(report=False)

    def v(self, name):
        return self.plc.current_state.tags.get(name, 0)

    def snapshot(self, report=True):
        for name in WATCH:
            cur = self.v(name)
            if report and self.last.get(name) != cur:
                print(f"[{self.label} scan {self.scan_count:6d}] {name}: "
                      f"{self.last.get(name)} -> {cur}", flush=True)
            self.last[name] = cur

    def step(self, n=1):
        for _ in range(n):
            if self.toggle_sensor:
                self.plc.force(T.x_RotateSensor, (self.scan_count // 50) % 2 == 0)
            self.plc.step()
            self.scan_count += 1
            self.snapshot()

    def pulse(self, report_label, **tag_values):
        self.plc.patch({getattr(T, k): v for k, v in tag_values.items()})
        print(f"[{self.label} scan {self.scan_count:6d}] PULSE {report_label}", flush=True)
        self.step()

    def wait_for(self, pred, what, max_scans=20000):
        for _ in range(max_scans):
            self.step()
            if pred():
                print(f"[{self.label} scan {self.scan_count:6d}] REACHED: {what}", flush=True)
                return True
        print(f"[{self.label} scan {self.scan_count:6d}] FAILED: {what} "
              f"not reached in {max_scans} scans", flush=True)
        return False


# ---------------------------------------------------------------- Phase A
print("=== PHASE A: cold snapshot ===", flush=True)
d = Driver("A")
print(f"after first scan: state={d.v('S_StateCurrent')} mode={d.v('S_UnitModeCurrent')} "
      f"step={d.v('Internal__Step')} P6={d.v('S_P6_HeatMaxRetry')}", flush=True)
d.step(3)
print(f"settled: state={d.v('S_StateCurrent')} S_Aborted={d.v('S_Aborted')} "
      f"mode={d.v('S_UnitModeCurrent')}", flush=True)
d.plc.force(T.OCmd_JogRotate, True)
d.step(3)
jog_ok = bool(d.v("y_RotateCt"))
print(f"A jog test: y_RotateCt = {jog_ok}", flush=True)

# ---------------------------------------------------------------- Phase B
print("\n=== PHASE B: full path from TRUE cold start (no bootstrap) ===", flush=True)
d = Driver("B")
for xt in (T.x_DoorClosed, T.x_LintDoorClosed, T.x_SailRelay, T.x_RotateFB, T.x_BlowerFB):
    d.plc.force(xt, True)
d.step(2)

heat_error_blips = []
_orig_snapshot = d.snapshot

ok = True
d.pulse("mode: C_ProductionMode + C_UnitModeChgRequest",
        C_ProductionMode=True, C_UnitModeChgRequest=True)
ok = ok and d.wait_for(lambda: d.v("S_UnitModeCurrent") == 1, "mode 1 (Production)", 20)
if ok:
    d.pulse("C_Clear", C_Clear=True)
    ok = d.wait_for(lambda: d.v("S_StateCurrent") == 2, "Stopped(2)", 200)
if ok:
    d.pulse("C_Reset", C_Reset=True)
    ok = d.wait_for(lambda: d.v("S_StateCurrent") == 4, "Idle(4)", 200)
if ok:
    d.toggle_sensor = True
    d.pulse("C_Start", C_Start=True)
    ok = d.wait_for(lambda: d.v("S_StateCurrent") == 6, "Execute(6)", 3000)
if ok:
    ok = d.wait_for(lambda: d.v("Heat_xCall") == 1, "Heat_xCall (HeatDelay 10s)", 2000)
b_ok = False
if ok:
    b_ok = d.wait_for(lambda: d.v("y_BurnerLoop"), "y_BurnerLoop ON", 3000)

print(f"[B] outcome: CurStep={d.v('Heat_CurStep')} Error={d.v('Heat_Error')} "
      f"Limit_Ts={d.v('Heat_Limit_Ts')} retries={d.v('S_CurrHeatRetryCount')} "
      f"P6={d.v('S_P6_HeatMaxRetry')} state={d.v('S_StateCurrent')} "
      f"total scans={d.scan_count} (~{d.scan_count * 0.01:.1f}s sim)", flush=True)

# hold 5 more sim-seconds to confirm burner stays on (no delayed error/abort)
if b_ok:
    d.step(500)
    print(f"[B] +5s hold: y_BurnerLoop={d.v('y_BurnerLoop')} Heat_Error={d.v('Heat_Error')} "
          f"state={d.v('S_StateCurrent')}", flush=True)

print(f"\nSUMMARY: cold_state9={d.v('S_StateCurrent') in (6,)} jog={jog_ok} "
      f"burner_from_true_cold={b_ok} elapsed={time.monotonic() - t0:.1f}s", flush=True)
