"""Barebones: is the reached fork itself the replayable artifact?

Grabs the reached fork ('work') off the finished PilotEvent — the thing
pilot_how computes and then throws away — and asks three questions:

  1. Did the DRIVE actually reach the target?  (is pilot borked?)
  2. What holds does the fork still carry in _synthesis.holds at the end?
  3. Does replay_to(tip) reproduce y_BurnerLoop straight from the fork's
     scan_log + _synthesis — no Path, no reactive-hold re-hydration?

If (1) and (3) are True, the fork IS the recording and Path is pure overhead.
If (3) is False, we've found exactly where the coast oscillators get lost.
"""

from __future__ import annotations

import os
import sys
import time
from pathlib import Path

CLICK_PROJECT = Path(
    os.environ.get(
        "PYRUNG_CLICK_PROJECT",
        r"C:\Users\Sam\AppData\Local\Temp\CLICK (0009051C)\pyrung_project",
    )
)
sys.path.insert(0, str(CLICK_PROJECT))

from main import logic  # noqa: E402
from tags import S_ProductionMode  # noqa: E402

from pyrung import PLC  # noqa: E402
from pyrung.core.analysis.pilot import pilot_events  # noqa: E402
from pyrung.core.analysis.prove import _compile_property  # noqa: E402

WATCH = ("S_UnitModeCurrent", "S_StateCurrent", "Heat_CurStep", "o_BurnerLoop", "y_BurnerLoop")


def show(tags, label):
    print(f"  {label}: " + "  ".join(f"{k}={tags.get(k)!r}" for k in WATCH))


plc = PLC(logic)
plc.step()
target = plc._known_tags_by_name["y_BurnerLoop"]
via_pred, _, _ = _compile_property(S_ProductionMode)

t0 = time.monotonic()
work = None
reached = None
for ev in pilot_events(plc, target, via_pred=via_pred, max_scans=100000):
    if ev.kind == "finished":
        work = ev.data["work"]
        reached = ev.data["reached"]
        break
print(f"[drive elapsed {time.monotonic() - t0:.1f}s]\n")

# 1) Did the drive reach?
print(f"1) finished reached flag : {reached}")
print(f"   fork tip scan_id      : {work.state.scan_id}")
show(work.state.tags, "fork end state")
drive_hit = work.state.tags.get("y_BurnerLoop") is True
print(f"   => fork actually at y_BurnerLoop=True? {drive_hit}\n")

# 2) What holds does the fork still carry?
syn = work._synthesis
holds = list(getattr(syn, "holds", []) or []) if syn is not None else []
plant = list(getattr(syn, "plant", []) or []) if syn is not None else []
print(f"2) work._synthesis        : {syn!r}")
print(f"   _synthesis.holds       : {len(holds)} rung(s)")
for r in holds:
    print(f"     - {r!r}")
print(f"   _synthesis.plant       : {len(plant)} rung(s)\n")

# 3) Does replay_to reproduce from the recording alone?
try:
    replayed = work.replay_to(work.state.scan_id)
    show(replayed.state.tags, "replay_to(tip) result")
    replay_hit = replayed.state.tags.get("y_BurnerLoop") is True
    print(f"   => replay reproduces y_BurnerLoop=True? {replay_hit}")
except Exception as exc:  # noqa: BLE001
    print(f"   replay_to raised: {type(exc).__name__}: {exc}")
    replay_hit = None

print("\n" + "#" * 70)
print(f"drive reached target : {drive_hit}")
print(f"recording replays    : {replay_hit}")
if drive_hit and replay_hit:
    print("=> The fork IS the artifact. Path is pure overhead.")
elif drive_hit and replay_hit is False:
    print("=> Drive is sound, but the raw recording loses the coast — the")
    print("   oscillator holds aren't persisted for replay. That's the seam to fix.")
elif not drive_hit:
    print("=> Pilot did NOT actually reach on the fork. This is a pilot bug, not a Path bug.")
