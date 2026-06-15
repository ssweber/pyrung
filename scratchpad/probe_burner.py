"""Probe: how(y_BurnerLoop) on the live template.

Expected: honest NotFound at ~120s — blocked on #9 rotate toggle.
Debug walk logger to file to see what the walker actually tries.
"""

import logging
import sys
import time

PROJECT = r"C:\Users\ssweb\AppData\Local\Temp\CLICK (000A0188)\pyrung_project"
sys.path.insert(0, PROJECT)

LOG = r"scratchpad\probe_burner_out\burner_walkdebug.txt"
handler = logging.FileHandler(LOG, mode="w", encoding="utf-8")
handler.setFormatter(logging.Formatter("%(relativeCreated)8.0f %(name)s %(message)s"))
walk_logger = logging.getLogger("pyrung.core.analysis.walk")
walk_logger.setLevel(logging.DEBUG)
walk_logger.addHandler(handler)

from main import logic  # noqa: E402
from tags import y_BurnerLoop  # noqa: E402

from pyrung import PLC  # noqa: E402

plc = PLC(logic)
plc.step()
t0 = time.monotonic()
print("calling how(y_BurnerLoop)...", flush=True)

path = plc.how(y_BurnerLoop, walk_seconds=120)
print(f"how() returned in {time.monotonic() - t0:.1f}s  reachable={path.reachable}", flush=True)
print(str(path)[:2000])
if getattr(path, "diagnosis", None) is not None:
    print(path.diagnosis)
