"""Run how(y_BurnerLoop) on the tumbler fixture and print the decision trace.

Compare against scratchpad/burner/ground_truth_y_burnerloop.py:
  Execute(6) ~816, OffWD Done bump 1316, alarm-11 1816, eject 6->8->9 at
  1852-54, goal y_BurnerLoop at ~2017 (with the sensor oscillated).

Run:  PYTHONPATH=. uv run python scratchpad/burner/drive_y_burnerloop.py [max_scans] [wall_s]
"""

from __future__ import annotations

import importlib
import json
import sys
import time

from pyrung import PLC
from pyrung.core.analysis.pilot import pilot_events

MAX_SCANS = int(sys.argv[1]) if len(sys.argv) > 1 else 40_000
WALL_S = float(sys.argv[2]) if len(sys.argv) > 2 else 300.0

SHOW = {
    "started",
    "candidate_accepted",
    "batch_accepted",
    "widening_accepted",
    "bearing_coast",
    "bearing_coast_accepted",
    "bearing_coast_rejected",
    "letrun_ejection",
    "trend_checkpoint",
    "trend_regression",
    "pending_departure_started",
    "pending_departure_promoted",
    "pending_departure_regressed",
    "pending_departure_expired",
    "skiff",
    "stuck",
    "finished",
}


def brief(kind: str, data: dict) -> str:
    if kind == "candidate_accepted":
        cd = data.get("candidate_detail") or {}
        return f"tag={cd.get('tag')} value={cd.get('value')!r} applied={data.get('applied')}"
    if kind == "bearing_coast":
        return f"reason={data.get('reason')!r} channel={data.get('channel_tag')}"
    if kind == "bearing_coast_accepted":
        return (
            f"label={data.get('observe_label')} outcome={data.get('outcome')} "
            f"chan={data.get('bearing_coast_channel_tag')} "
            f"target={data.get('bearing_coast_target_value')!r} "
            f"landed={data.get('bearing_coast_actual_value')!r} ejected={data.get('ejected')} "
            f"trend={data.get('trend')} scans={data.get('scan_before')}->{data.get('scan_after')}"
        )
    if kind == "bearing_coast_rejected":
        return f"gates={[(g.event, g.detail[:60]) for g in data.get('gates', ())]}"
    if kind == "letrun_ejection":
        return (
            f"chan={data.get('channel_tag')} {data.get('from_value')!r}->"
            f"{data.get('to_value')!r} (req {data.get('requested_value')!r}) "
            f"span={data.get('coast_span')} investigated={data.get('investigated')}"
        )
    if kind == "trend_regression":
        inv = data.get("investigation") or {}
        lines = [
            f"trend {data.get('from_trend')}->{data.get('to_trend')} "
            f"hyps={inv.get('hypotheses')} confirmed={inv.get('confirmed')} "
            f"rejected={inv.get('rejected')}"
        ]
        for h in inv.get("confirmed_detail", ()):
            lines.append(f"      CONFIRMED {h.get('kind')}: {h.get('detail')}")
        for h in inv.get("rejected_detail", ()):
            lines.append(
                f"      rejected[{h.get('slug')}] {h.get('kind')}: "
                f"{str(h.get('detail'))[:90]} | ground: {str(h.get('ground'))[:90]}"
            )
        return "\n".join(lines)
    if kind in ("trend_checkpoint",):
        return f"trend={data.get('trend')} chan={data.get('channel')}={data.get('channel_value')!r}"
    if kind == "finished":
        return f"reached={data.get('reached')} reason={data.get('reason')!r}"
    if kind == "stuck":
        return f"reason={data.get('reason')!r}"
    if kind.startswith("pending_departure"):
        return (
            f"chan={data.get('channel_tag')} from={data.get('from_value')!r} "
            f"reason={str(data.get('reason'))[:80]!r}"
        )
    if kind == "skiff":
        return f"reason={str(data.get('reason'))[:80]!r}"
    if kind == "started":
        return f"target={data.get('target')}"
    return json.dumps({k: str(v)[:40] for k, v in list(data.items())[:4]})


def main() -> None:
    logic = importlib.import_module("tests.fixtures.tumbler").logic
    plc = PLC(logic, dt=0.010)
    plc.step()
    target = plc._known_tags_by_name["y_BurnerLoop"]
    t0 = time.perf_counter()
    count = 0
    for event in pilot_events(plc, target, max_scans=MAX_SCANS):
        count += 1
        wall = time.perf_counter() - t0
        if event.kind in SHOW:
            print(f"[{wall:7.1f}s] scan {event.scan:6d} {event.kind:20s} {brief(event.kind, dict(event.data))}")
        if event.kind == "finished":
            break
        if wall > WALL_S:
            print(f"[{wall:7.1f}s] WALL BUDGET {WALL_S}s EXCEEDED — aborting drive loop")
            break
    print(f"\n{count} events, {time.perf_counter() - t0:.1f}s wall")


if __name__ == "__main__":
    main()
