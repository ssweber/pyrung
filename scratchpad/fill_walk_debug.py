"""Debug: exercise how() on the fill-station project with walk DEBUG logging.

Goal: understand why the walk output is patchy (some tags reachable, others
not, or plans that look incomplete).  Captures the full walk journal/logger
output for each target.
"""

import logging
import sys
import time

PROJECT = r"C:\Users\ssweb\AppData\Local\Temp\CLICK (00010C0A)\pyrung_project"
sys.path.insert(0, PROJECT)

logging.basicConfig(
    level=logging.DEBUG,
    format="%(name)s %(levelname)s  %(message)s",
    stream=sys.stdout,
)
walk_logger = logging.getLogger("pyrung.core.analysis.walk")
walk_logger.setLevel(logging.DEBUG)

from main import logic  # noqa: E402
from tags import (  # noqa: E402
    HMI_on,
    HMI_fill,
    HMI_resetError,
    _fill,
    alarm,
    alarm_fillTimeout,
    alarm_levelMaxHt,
    buzzer,
    fill_solv_nc,
    fill_stepNumber,
    sl_blue,
    sl_buzzer,
    sl_green,
    sl_red,
    sl_yellow,
    sub_fillFilling,
    sub_fillOff,
    sub_fillOn,
    warn,
    warn_fillSlow,
    warn_levelMinHt,
)

from pyrung import PLC  # noqa: E402

plc = PLC(logic)
plc.step()

targets = [
    ("fill_solv_nc", fill_solv_nc),
    ("fill_solv_nc (avoid HMI_fill)", (fill_solv_nc,), {"avoid": HMI_fill}),
    ("sl_blue", sl_blue),
    ("sl_red", sl_red),
    ("sl_green", sl_green),
    ("sl_yellow", sl_yellow),
    ("alarm", alarm),
    ("warn", warn),
    ("fill_stepNumber == 3", fill_stepNumber == 3),
    ("fill_stepNumber == 5", fill_stepNumber == 5),
    ("sub_fillOn", sub_fillOn),
    ("sub_fillFilling", sub_fillFilling),
]

results = []
for entry in targets:
    if isinstance(entry, tuple) and len(entry) == 3:
        label, conds, kwargs = entry
    else:
        label, conds = entry[0], entry[1]
        kwargs = {}

    if not isinstance(conds, tuple):
        conds = (conds,)

    print(f"\n{'='*72}")
    print(f"TARGET: {label}")
    print(f"{'='*72}", flush=True)

    t0 = time.monotonic()
    path = plc.how(*conds, walk_seconds=30, **kwargs)
    elapsed = time.monotonic() - t0

    status = "REACHABLE" if path.reachable else "NOT REACHABLE"
    reason = getattr(path, "reason", None) or ""
    n_steps = len(path.steps) if path.steps else 0

    print(f"\n  Result: {status}  steps={n_steps}  time={elapsed:.1f}s")
    if reason:
        print(f"  Reason: {reason}")
    if path.reachable and path.steps:
        print(f"  Path:\n{str(path)[:1500]}")
    diag = getattr(path, "diagnosis", None)
    if diag:
        print(f"  Diagnosis: {diag}")

    results.append((label, status, n_steps, elapsed, reason))

print(f"\n\n{'='*72}")
print("SUMMARY")
print(f"{'='*72}")
for label, status, n_steps, elapsed, reason in results:
    tag = "OK" if status == "REACHABLE" else "FAIL"
    print(f"  [{tag:4s}] {label:40s}  steps={n_steps:2d}  {elapsed:5.1f}s  {reason}")
