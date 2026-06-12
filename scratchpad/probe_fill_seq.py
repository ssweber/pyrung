"""Probe: bisect the fill sequencer walk failure.

Ground truth first: patch HMI_on + systemLevel_opt2011 + sv_levelBand and
run — does fill_stepNumber reach 5 / does _fill come on?

Then the how() ladder: subStatus==1, step==3, step==5, _fill.
"""

import sys
import time

PROJECT = r"C:\Users\Sam\AppData\Local\Temp\CLICK (0175103C)\pyrung_project"
sys.path.insert(0, PROJECT)

from main import logic  # noqa: E402
from tags import (  # noqa: E402
    HMI_on,
    _fill,
    fill_stepNumber,
    fill_subStatus,
    sv_levelBand,
    systemLevel_opt2011,
    t_fillDelay,
)

from pyrung import PLC  # noqa: E402

plc = PLC(logic)
plc.step()

# --- ground truth ---
f = plc.fork()
f.patch({HMI_on: True, systemLevel_opt2011: 100.0, sv_levelBand: -5.0})
for i in range(40):
    f.step()
    t = f.state.tags
    if i < 12 or t.get("fill_stepNumber") == 5:
        print(
            f"scan {i}: step={t.get('fill_stepNumber')} subStatus={t.get('fill_subStatus')}"
            f" pv={t.get('pv_LevelHt')} lower={t.get('calc_levelSvLowerWBand')}"
            f" tDelayDone={t.get(t_fillDelay.Done.name)} _fill={t.get('_fill')}"
        )
    if t.get("_fill"):
        print(f"GROUND TRUTH: _fill=True at scan {i}")
        break
else:
    print("GROUND TRUTH: _fill NOT reached in 40 scans")

# --- how() ladder ---
CASES = [
    ("subStatus==1", fill_subStatus == 1),
    ("step==3", fill_stepNumber == 3),
    ("step==4", fill_stepNumber == 4),
    ("step==5", fill_stepNumber == 5),
]
for label, cond in CASES:
    t0 = time.monotonic()
    path = plc.how(cond, walk_seconds=60)
    dt = time.monotonic() - t0
    print(f"\n=== how({label}): {dt:.1f}s reachable={path.reachable} ===", flush=True)
    if path.reachable:
        print(str(path)[:1200])
    else:
        diag = getattr(path, "diagnosis", None)
        if diag is not None:
            print(str(diag)[:1200])
