"""Smoke test: how(S_StateCurrent == 4) on the current template.

Notebook says this solves reachable=True in ~3.5s post-aliasing-fix.
"""

import sys
import time

PROJECT = r"C:\Users\ssweb\AppData\Local\Temp\CLICK (000A0188)\pyrung_project"
sys.path.insert(0, PROJECT)

from main import logic  # noqa: E402
from tags import S_StateCurrent  # noqa: E402

from pyrung import PLC  # noqa: E402

plc = PLC(logic)
plc.step()
print(f"cold: S_StateCurrent={plc.state.tags['S_StateCurrent']}", flush=True)

t0 = time.monotonic()
path = plc.how(S_StateCurrent == 4, walk_seconds=30)
elapsed = time.monotonic() - t0
print(f"how() returned in {elapsed:.1f}s  reachable={path.reachable}", flush=True)
print(str(path)[:2000])
