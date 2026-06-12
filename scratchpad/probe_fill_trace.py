"""Trace walker goal chain for how(fill_stepNumber == 4) on the fill project."""

import logging
import sys

PROJECT = r"C:\Users\Sam\AppData\Local\Temp\CLICK (0175103C)\pyrung_project"
sys.path.insert(0, PROJECT)

logging.basicConfig(level=logging.DEBUG, format="%(name)s %(message)s")
for noisy in ("pyrung.core.analysis.prove", "pyrung.core.runner"):
    logging.getLogger(noisy).setLevel(logging.WARNING)

from main import logic  # noqa: E402
from tags import fill_stepNumber  # noqa: E402

from pyrung import PLC  # noqa: E402

plc = PLC(logic)
plc.step()

path = plc.how(fill_stepNumber == 5, max_steps=80, walk_seconds=60)
print(f"\nreachable={path.reachable}")
if not path.reachable and path.diagnosis:
    print(str(path.diagnosis)[:800])
