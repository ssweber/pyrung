"""Probe 22: idx-chasing + calc-scratch hop on the live template.

Re-run the probe17/18 shape (how(S_StateCurrent == 4), walk_seconds=120).
Probe18 (chase without the hop) still failed at ``sm__where2jump -> 4``:
the pointer is calc-defined scratch (``calc(S_StateRequested + 150,
sm__jump_target_ds_idx)``), slice-elided by the pipeline, so the chase had
no candidates.  With the hop the sub-goal should land on S_StateRequested
and the goal either resolves or fails deeper in the chain.
"""

import logging
import sys
import time

PROJECT = r"C:\Users\Sam\AppData\Local\Temp\CLICK (005E0E4C)\pyrung_project"
sys.path.insert(0, PROJECT)

from main import logic  # noqa: E402
from tags import S_StateCurrent  # noqa: E402

from pyrung import PLC  # noqa: E402

LOG = r"scratchpad\probe_burner_out\probe22_walkdebug.txt"

handler = logging.FileHandler(LOG, mode="w", encoding="utf-8")
handler.setFormatter(logging.Formatter("%(relativeCreated)8.0f %(name)s %(message)s"))
walk_logger = logging.getLogger("pyrung.core.analysis.walk")
walk_logger.setLevel(logging.DEBUG)
walk_logger.addHandler(handler)

plc = PLC(logic)
plc.step()
t0 = time.monotonic()
path = plc.how(S_StateCurrent == 4, walk_seconds=120)
print(f"returned in {time.monotonic() - t0:.1f}s reachable={path.reachable}", flush=True)
print(str(path)[:2000])
