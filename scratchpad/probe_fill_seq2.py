"""Probe: step==5, _fill, and the compound goal, with max_steps=80."""
import sys
import time

PROJECT = r"C:\Users\Sam\AppData\Local\Temp\CLICK (0175103C)\pyrung_project"
sys.path.insert(0, PROJECT)

from main import logic  # noqa: E402
from tags import HMI_fill, _fill, fill_solv_nc, fill_stepNumber  # noqa: E402

from pyrung import PLC  # noqa: E402

plc = PLC(logic)
plc.step()

CASES = [
    ("step==5", (fill_stepNumber == 5,)),
    ("_fill", (_fill,)),
    ("fill_solv_nc, ~HMI_fill", (fill_solv_nc, ~HMI_fill)),
]
for label, conds in CASES:
    t0 = time.monotonic()
    path = plc.how(*conds, max_steps=80, walk_seconds=120)
    dt = time.monotonic() - t0
    print(f"\n=== how({label}): {dt:.1f}s reachable={path.reachable} ===", flush=True)
    if path.reachable:
        print(str(path)[:1500])
    else:
        diag = getattr(path, "diagnosis", None)
        if diag is not None:
            print(str(diag)[:1200])
