"""Capture per-coast fold stats (receipt debug lines) on the avoid-gate drive."""

from __future__ import annotations

import importlib
import logging
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
logging.basicConfig(level=logging.WARNING, format="%(name)s %(message)s")
for name in ("pyrung.core.analysis.pilot.coast",):
    logging.getLogger(name).setLevel(logging.DEBUG)

logic = importlib.import_module("tests.fixtures.tumbler").logic

from pyrung import PLC  # noqa: E402
from pyrung.core.analysis.pilot import pilot_events  # noqa: E402
from pyrung.core.runner import _compile_avoid  # noqa: E402

wall = float(sys.argv[1]) if len(sys.argv) > 1 else 75.0
plc = PLC(logic)
plc.step()
tags = plc._known_tags_by_name
avoid_pred = _compile_avoid(tags["Cmd_State_Complete"])
deadline = time.monotonic() + wall
for event in pilot_events(plc, tags["Sts_StateCurrent"] == 17, max_scans=40_000, avoid_pred=avoid_pred):
    if event.kind == "finished" or time.monotonic() > deadline:
        break
