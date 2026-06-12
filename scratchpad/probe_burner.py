"""Instrumented reproduction of `how y_BurnerLoop` on the CLICK (0032023C) project.

Logs walk/runner activity with timestamps, dumps all-thread stacks every 20s
via faulthandler, and samples RSS every 5s. Kill externally when satisfied.
"""

import faulthandler
import logging
import os
import sys
import threading
import time

PROJECT = r"C:\Users\Sam\AppData\Local\Temp\CLICK (0032023C)\pyrung_project"
OUT_DIR = os.path.join(os.path.dirname(__file__), "probe_burner_out")
os.makedirs(OUT_DIR, exist_ok=True)

sys.path.insert(0, PROJECT)

# --- logging -----------------------------------------------------------
log_path = os.path.join(OUT_DIR, "walk.log")
handler = logging.FileHandler(log_path, mode="w", encoding="utf-8")
handler.setFormatter(logging.Formatter("%(asctime)s %(name)s %(levelname)s %(message)s"))
for name in ("pyrung.core.runner", "pyrung.core.analysis.walk", "pyrung.core.analysis.prove"):
    lg = logging.getLogger(name)
    lg.setLevel(logging.DEBUG)
    lg.addHandler(handler)

# --- stack dumps -------------------------------------------------------
stacks = open(os.path.join(OUT_DIR, "stacks.txt"), "w", encoding="utf-8")
faulthandler.dump_traceback_later(20, repeat=True, file=stacks)

# --- RSS sampling ------------------------------------------------------
def _rss_watcher():
    import ctypes
    import ctypes.wintypes as wt

    class PMC(ctypes.Structure):
        _fields_ = [
            ("cb", wt.DWORD),
            ("PageFaultCount", wt.DWORD),
            ("PeakWorkingSetSize", ctypes.c_size_t),
            ("WorkingSetSize", ctypes.c_size_t),
            ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
            ("QuotaPagedPoolUsage", ctypes.c_size_t),
            ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
            ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
            ("PagefileUsage", ctypes.c_size_t),
            ("PeakPagefileUsage", ctypes.c_size_t),
        ]

    psapi = ctypes.WinDLL("psapi")
    kernel32 = ctypes.WinDLL("kernel32")
    handle = kernel32.GetCurrentProcess()
    with open(os.path.join(OUT_DIR, "rss.log"), "w", encoding="utf-8") as f:
        t0 = time.monotonic()
        while True:
            pmc = PMC()
            pmc.cb = ctypes.sizeof(PMC)
            psapi.GetProcessMemoryInfo(handle, ctypes.byref(pmc), pmc.cb)
            f.write(f"{time.monotonic() - t0:8.1f}s  rss={pmc.WorkingSetSize / 1e6:9.1f} MB\n")
            f.flush()
            time.sleep(5)


threading.Thread(target=_rss_watcher, daemon=True).start()

# --- the reproduction --------------------------------------------------
print("importing program...", flush=True)
t0 = time.monotonic()
from main import logic  # noqa: E402
from tags import y_BurnerLoop  # noqa: E402

from pyrung import PLC  # noqa: E402

print(f"import done in {time.monotonic() - t0:.1f}s", flush=True)

plc = PLC(logic)
plc.step()
print(f"first scan done at {time.monotonic() - t0:.1f}s; calling how()...", flush=True)

path = plc.how(y_BurnerLoop)
print(f"how() returned at {time.monotonic() - t0:.1f}s", flush=True)
print(path)
if getattr(path, "diagnosis", None) is not None:
    print(path.diagnosis)
