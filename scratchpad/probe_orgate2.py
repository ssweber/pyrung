"""Or-gate invisible to prereqs, branches needing multi-scan corridors.

S_A's producer (R1) is ABOVE Prep's producer (R2), so a single edge blast
cannot latch S_A — Prep must be latched on an earlier scan. Ground truth:
pulse CmdP, pulse CmdA, pulse Go.
"""

import logging
import sys
import time

from pyrung import Bool, Or, Program, Rung, latch, out, rise
from pyrung.core.runner import PLC

logging.basicConfig(stream=sys.stdout, level=logging.INFO, format="%(name)s %(message)s")
logging.getLogger("pyrung.core.analysis.walk").setLevel(logging.DEBUG)

CmdP = Bool("CmdP", external=True)
CmdA = Bool("CmdA", external=True)
CmdB = Bool("CmdB", external=True)
Go = Bool("Go", external=True)
Prep = Bool("Prep")
S_A = Bool("S_A")
S_B = Bool("S_B")
Done = Bool("Done")
Target = Bool("Target")

with Program() as prog:
    with Rung(rise(CmdA), Prep):
        latch(S_A)
    with Rung(rise(CmdP)):
        latch(Prep)
    with Rung(rise(CmdB), S_A):
        latch(S_B)
    with Rung(rise(Go), Or(S_A, S_B)):
        latch(Done)
    with Rung(Done):
        out(Target)

# Ground truth.
plc = PLC(prog, dt=0.010)
plc.step()
for name in ("CmdP", "CmdA", "Go"):
    plc.patch({name: True})
    plc.step()
    plc.patch({name: False})
    plc.step()
print(f"ground truth: S_A={plc.state.tags['S_A']}, Target={plc.state.tags['Target']}")
assert plc.state.tags["Target"] is True

plc2 = PLC(prog, dt=0.010)
plc2.step()
t0 = time.monotonic()
path = plc2.how(Target, walk_seconds=30)
print(f"how() in {time.monotonic() - t0:.1f}s")
print(path)
