"""Isolate the rotate-sensor round: pre-force the permissives the pilot would
have earned (doors + both FBs), leave x_RotateSensor dead, run how(y_BurnerLoop).

Ground truth (trajectory B): Execute at ~816, Rotate_SensorOffWD_tmr.Done at
~1316 (pen mark), state 6->8 at ~1852, ABORTED ~1854, goal needs ~2017.
Expected: let-run/bearing-coast ejection from 6, incident carrying the OffWD Done pen,
liveness OSCILLATE (or steady FLIP + complement round) on x_RotateSensor,
then the drive completes.

Run:  PYTHONPATH=. uv run python scratchpad/burner/drive_rotate_round.py
"""

from __future__ import annotations

import importlib
import time

from pyrung import PLC
from pyrung.core.analysis.pilot import pilot_events

WALL_S = 480.0

FORCED = {
    "x_DoorClosed": True,
    "x_LintDoorClosed": True,
    "x_BlowerFB": True,
    "x_RotateFB": True,
    "x_SailRelay": True,
}


def main() -> None:
    logic = importlib.import_module("tests.fixtures.tumbler").logic
    plc = PLC(logic, dt=0.010)
    for name, value in FORCED.items():
        plc.force(name, value)
    plc.step()
    target = plc._known_tags_by_name["y_BurnerLoop"]
    t0 = time.perf_counter()
    for event in pilot_events(plc, target, max_scans=40_000):
        wall = time.perf_counter() - t0
        kind = event.kind
        data = dict(event.data)
        if kind in ("candidate_accepted",):
            cd = data.get("candidate_detail") or {}
            print(f"[{wall:6.1f}s] scan {event.scan:6d} accept {cd.get('tag')}={cd.get('value')!r}")
        elif kind in ("bearing_coast",):
            print(
                f"[{wall:6.1f}s] scan {event.scan:6d} "
                f"bearing coast   {data.get('reason')!r}"
            )
        elif kind == "bearing_coast_accepted":
            print(
                f"[{wall:6.1f}s] scan {event.scan:6d} bearing-coast OK "
                f"chan={data.get('bearing_coast_channel_tag')}"
                f" target={data.get('bearing_coast_target_value')!r} "
                f"landed={data.get('bearing_coast_actual_value')!r}"
                f" ejected={data.get('ejected')} scans={data.get('scan_before')}->{data.get('scan_after')}"
            )
        elif kind == "bearing_coast_rejected":
            print(
                f"[{wall:6.1f}s] scan {event.scan:6d} bearing-coast NO "
                f"{[(g.event, g.detail[:50]) for g in data.get('gates', ())]}"
            )
        elif kind == "letrun_ejection":
            print(
                f"[{wall:6.1f}s] scan {event.scan:6d} EJECT  {data.get('channel_tag')}"
                f" {data.get('from_value')!r}->{data.get('to_value')!r} span={data.get('coast_span')}"
            )
        elif kind == "trend_regression":
            inv = data.get("investigation") or {}
            print(f"[{wall:6.1f}s] scan {event.scan:6d} INVEST hyps={inv.get('hypotheses')} confirmed={inv.get('confirmed')}")
            for h in inv.get("confirmed_detail", ()):
                print(f"          CONFIRMED {h.get('kind')}: {h.get('detail')}")
            for h in inv.get("rejected_detail", ()):
                print(f"          rejected[{h.get('slug')}] {h.get('kind')}: {str(h.get('detail'))[:80]}")
        elif kind in ("stuck", "finished"):
            print(f"[{wall:6.1f}s] scan {event.scan:6d} {kind.upper()} reached={data.get('reached')} reason={str(data.get('reason'))[:140]!r}")
        if kind == "finished" or wall > WALL_S:
            break
    print(f"done {time.perf_counter() - t0:.1f}s")


if __name__ == "__main__":
    main()
