"""Minimal Or-gate-invisible-to-prereqs shape (findings §2b).

Done's writer is gated on Or(S_A, S_B) — different tags per branch, so
_extract_condition_values drops the whole Or and no state goal is ever
spawned. Ground truth: pulse CmdA (latches S_A), then pulse Go.
"""

import logging
import sys
import time

from pyrung import Bool, Or, Program, Rung, latch, out, rise
from pyrung.core.runner import PLC

logging.basicConfig(stream=sys.stdout, level=logging.INFO, format="%(name)s %(message)s")
logging.getLogger("pyrung.core.analysis.walk").setLevel(logging.DEBUG)

CmdA = Bool("CmdA", external=True)
CmdB = Bool("CmdB", external=True)
Go = Bool("Go", external=True)
S_A = Bool("S_A")
S_B = Bool("S_B")
Done = Bool("Done")
Target = Bool("Target")

with Program() as prog:
    with Rung(rise(CmdA)):
        latch(S_A)
    with Rung(rise(CmdB)):
        latch(S_B)
    with Rung(rise(Go), Or(S_A, S_B)):
        latch(Done)
    with Rung(Done):
        out(Target)

# Ground truth.
plc = PLC(prog, dt=0.010)
plc.step()
plc.patch({"CmdA": True})
plc.step()
plc.patch({"Go": True})
plc.step()
print(f"ground truth: S_A={plc.state.tags['S_A']}, Target={plc.state.tags['Target']}")
assert plc.state.tags["Target"] is True

plc2 = PLC(prog, dt=0.010)
plc2.step()
t0 = time.monotonic()
path = plc2.how(Target, walk_seconds=30)
print(f"how() in {time.monotonic() - t0:.1f}s")
print(path)
