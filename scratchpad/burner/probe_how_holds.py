"""Confirm the how() path isn't recording its holds.

Runs how() once (debug=True) and dumps everything hold-related on the returned
Path: path.holds, path.triangle, and each step's reactive_holds / attributes.
If PILOT relied on held/oscillated prerequisites during its coast but the Path
carries none, that is the "not recording its holds" gap — the bare steps can't
reproduce the target (S_StateCurrent stays stuck).
"""

from __future__ import annotations

import os
import sys
import time
from pathlib import Path as _P

CLICK_PROJECT = _P(
    os.environ.get(
        "PYRUNG_CLICK_PROJECT",
        r"C:\Users\Sam\AppData\Local\Temp\CLICK (0009051C)\pyrung_project",
    )
)
sys.path.insert(0, str(CLICK_PROJECT))

from main import logic  # noqa: E402
from tags import S_ProductionMode  # noqa: E402

from pyrung import PLC  # noqa: E402

plc = PLC(logic)
plc.step()
target = plc._known_tags_by_name["y_BurnerLoop"]

t0 = time.monotonic()
path = plc.how(target, via=S_ProductionMode, max_scans=100000, debug=True)
print(f"[how() elapsed {time.monotonic() - t0:.1f}s]\n")

print(f"reachable      : {path.reachable}")
print(f"total_scans    : {path.total_scans}")
print(f"path.holds     : {path.holds!r}")
print(f"path.triangle  : {path.triangle!r}")
print(f"path.route     : {getattr(path.route, 'label', None)!r}")
print()

print("Per-step detail:")
for i, step in enumerate(path.steps, 1):
    rh = getattr(step, "reactive_holds", None)
    constraints = getattr(step, "constraints", None)
    print(f"  step {i}: scans={step.scans}")
    print(f"    action        = {step.action!r}")
    print(f"    reactive_holds= {rh!r}")
    print(f"    constraints   = {constraints!r}")

# The journey shows what PILOT actually did, incl. holds it installed per attempt.
journey = path.journey or ()
print(f"\nJourney: {len(journey)} attempts")
holds_seen = 0
for i, step in enumerate(journey, 1):
    rh = getattr(step, "reactive_holds", None)
    if rh:
        holds_seen += 1
        print(f"  attempt {i}: scans={step.scans}  action={step.action!r}  reactive_holds={rh!r}")
print(f"\nattempts that carried reactive_holds: {holds_seen}/{len(journey)}")


# --- Decisive: replay the clean path, tracking transient pulses + final state ---
from pyrung.core.analysis.pilot._ops import _install_reactive_holds  # noqa: E402

FINAL = ("S_UnitModeCurrent", "S_StateCurrent", "Heat_CurStep", "o_BurnerLoop", "y_BurnerLoop")


def replay(with_holds: bool) -> dict:
    p = PLC(logic)
    gscan = 0
    y_first = None  # first scan y_BurnerLoop went True
    o_first = None  # first scan o_BurnerLoop (production SFC output) went True
    y_true_count = 0
    for i, step in enumerate(path.steps, 1):
        if step.action:
            p.patch(step.action)
        holds = getattr(step, "reactive_holds", None) if with_holds else None
        handles = _install_reactive_holds(p, holds) if holds else []
        try:
            for _ in range(step.scans):
                p.step()
                gscan += 1
                t = p.state.tags
                if t.get("y_BurnerLoop") is True:
                    y_true_count += 1
                    if y_first is None:
                        y_first = (i, gscan)
                if t.get("o_BurnerLoop") is True and o_first is None:
                    o_first = (i, gscan)
        finally:
            for h in handles:
                h.remove()
    t = p.state.tags
    return {
        "with_holds": with_holds,
        "y_ever": y_first,
        "y_true_count": y_true_count,
        "o_ever": o_first,
        "final": {k: t.get(k) for k in FINAL},
    }


for wh in (True, False):
    r = replay(with_holds=wh)
    print("\n" + "#" * 70)
    print(f"replay {'WITH' if wh else 'WITHOUT'} recorded reactive_holds")
    print("#" * 70)
    print(f"  y_BurnerLoop ever True? : {r['y_ever']}   (scans True: {r['y_true_count']})")
    print(f"  o_BurnerLoop ever True? : {r['o_ever']}")
    print(f"  final state             : {r['final']}")
