"""Probe: how(fill_solv_nc, ~HMI_fill) on the fill-tumbler project.

DAP console reported:
  Unreachable: walker: target not reachable
  Diagnosis: not-found -- goal HMI_fill -> False failed (goal-regressed)
  ... note: compound goals: must-stay regression -- retrying with
  HMI_fill==False before fill_solv_nc==True

fill_solv_nc = (_fill OR HMI_fill) AND ~alarm   (io R1)
_fill = sub_fillFilling                          (filling R2)
sub_fillFilling = fill_stepNumber == 5           (main R7)
So with HMI_fill held False the walker must walk the odd/even step
sequencer to step 5. Cases isolate where that breaks.
"""

import sys
import time

PROJECT = r"C:\Users\Sam\AppData\Local\Temp\CLICK (0175103C)\pyrung_project"
sys.path.insert(0, PROJECT)

from main import logic  # noqa: E402
from tags import HMI_fill, _fill, fill_solv_nc, fill_stepNumber, sub_fillFilling  # noqa: E402

from pyrung import PLC  # noqa: E402

plc = PLC(logic)
plc.step()
print(f"cold fill_stepNumber = {plc.state.tags.get('fill_stepNumber')}")
print(f"cold _fill           = {plc.state.tags.get('_fill')}")
print(f"cold fill_solv_nc    = {plc.state.tags.get('fill_solv_nc')}", flush=True)

CASES = [
    ("1 sequencer alone: _fill", (_fill,), {}),
    ("2 fill_solv_nc alone", (fill_solv_nc,), {}),
    ("3 fill_solv_nc, ~HMI_fill (DAP order)", (fill_solv_nc, ~HMI_fill), {}),
    ("4 ~HMI_fill, fill_solv_nc (reversed)", (~HMI_fill, fill_solv_nc), {}),
    ("5 fill_solv_nc avoid HMI_fill", (fill_solv_nc,), {"avoid": HMI_fill}),
]

for label, conds, kw in CASES:
    t0 = time.monotonic()
    path = plc.how(*conds, walk_seconds=120, **kw)
    dt = time.monotonic() - t0
    print(f"\n=== {label}: {dt:.1f}s reachable={path.reachable} ===", flush=True)
    if path.reachable:
        print(str(path)[:2000])
    else:
        print(f"reason: {getattr(path, 'reason', None)}")
        diag = getattr(path, "diagnosis", None)
        if diag is not None:
            print(str(diag)[:2000])
    print(flush=True)
