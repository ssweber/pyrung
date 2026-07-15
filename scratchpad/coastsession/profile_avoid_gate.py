"""Profile the avoid-shortcut internal-route drive — where do the 240s go?

Runs the same drive as test_pilot_internal_route_gate_completed_avoiding_shortcut
under cProfile with a wall cutoff, then dumps top cumulative sinks.

Run from repo root: uv run python scratchpad/coastsession/profile_avoid_gate.py [wall_s]
"""

from __future__ import annotations

import cProfile
import importlib
import io
import pstats
import sys
import time
from collections import Counter

WALL_S = float(sys.argv[1]) if len(sys.argv) > 1 else 100.0

from pathlib import Path  # noqa: E402

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
logic = importlib.import_module("tests.fixtures.tumbler").logic

from pyrung import PLC  # noqa: E402
from pyrung.core.analysis.pilot import pilot_events  # noqa: E402
from pyrung.core.runner import _compile_avoid  # noqa: E402

plc = PLC(logic)
plc.step()
tags = plc._known_tags_by_name
target = tags["Sts_StateCurrent"]
avoid_pred = _compile_avoid(tags["Cmd_State_Complete"])

kinds: Counter[str] = Counter()
last_scan = 0
prof = cProfile.Profile()
deadline = time.monotonic() + WALL_S
prof.enable()
for event in pilot_events(plc, target == 17, max_scans=40_000, avoid_pred=avoid_pred):
    kinds[event.kind] += 1
    last_scan = event.scan
    if event.kind == "finished" or time.monotonic() > deadline:
        break
prof.disable()

print(f"stopped at scan {last_scan}; event kinds: {dict(kinds)}")
buf = io.StringIO()
stats = pstats.Stats(prof, stream=buf)
stats.sort_stats("cumulative").print_stats(45)
lines = buf.getvalue().splitlines()
# Skip the header noise, keep the table
for line in lines[4:70]:
    print(line)
