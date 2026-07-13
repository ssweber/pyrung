"""Live frontier probe: how(S_StateCurrent==17, avoid=C_Complete).

Prints the coast/regression/stuck/finished event stream so the terminal
diagnostic is visible.  The known failure shape (2026-07-09): the Execute-wait
dwell (Dry step's S_HeatAtTemp_tmr never arms because S_DryerTemp_F sits at
0.0 with no feedback coupling) is coasted repeatedly and accepted back at
EXECUTE(6); the run ends with a nameless "budget exhausted" instead of a
pointable frontier.
"""

from __future__ import annotations

import logging
import os
import sys
import time
from pathlib import Path

logging.basicConfig(level=logging.WARNING, format="%(name)s %(message)s")
if os.environ.get("PILOT_DEBUG_LOGS"):
    logging.getLogger("pyrung.core.analysis.pilot.investigate").setLevel(logging.DEBUG)
    logging.getLogger("pyrung.core.analysis.pilot.detour").setLevel(logging.DEBUG)
    logging.getLogger("pyrung.core.analysis.pilot._ops").setLevel(logging.DEBUG)
    logging.getLogger("pyrung.core.analysis.pilot.charts").setLevel(logging.DEBUG)

CLICK_PROJECT = Path(
    os.environ.get(
        "PYRUNG_CLICK_PROJECT",
        r"C:\Users\Sam\AppData\Local\Temp\CLICK (00010A00)\pyrung_project",
    )
)
if not CLICK_PROJECT.is_dir():
    raise SystemExit(f"burner export does not exist: {CLICK_PROJECT}")
print(f"[EXPORT] {CLICK_PROJECT}", flush=True)
sys.path.insert(0, str(CLICK_PROJECT))

from main import logic  # noqa: E402

from pyrung import PLC  # noqa: E402
from pyrung.core.analysis.pilot import pilot_events  # noqa: E402
from pyrung.core.runner import _compile_avoid  # noqa: E402


def main() -> int:
    plc = PLC(logic)
    plc.step()
    # Bench test recipe: 1-minute dwells (Dry/Cool/Shine) so the run exercises
    # routing and the HELD handshake instead of grinding 60 simulated minutes
    # through the degraded fold (cyclefold efficiency is a separate work item).
    plc.patch(
        {
            "C_P2_Dry_Tm": 1,
            "C_P4_CoolDown_Tm": 1,
            "C_P3_Shine_Tm": 1,
            "S_P2_Dry_Tm": 1,
            "S_P4_Cooldown_Tm": 1,
            "S_P3_Shine_Tm": 1,
        }
    )
    plc.step()
    tags = plc._known_tags_by_name
    state_current = tags["S_StateCurrent"]
    c_complete = tags["C_Complete"]
    max_scans = int(os.environ.get("PILOT_MAX_SCANS", "400000"))

    t0 = time.perf_counter()
    finished = None
    for event in pilot_events(
        plc, state_current == 17, max_scans=max_scans, avoid_pred=_compile_avoid(c_complete)
    ):
        d = event.data
        k = event.kind
        if k == "started":
            route = d.get("route")
            print(
                f"[STARTED] route={getattr(route, 'label', None)} max_scans={max_scans}", flush=True
            )
        elif k == "candidate_accepted":
            print(
                f"[scan {event.scan}] ACCEPTED: {d['candidate']} applied={d.get('applied')}",
                flush=True,
            )
        elif k == "candidate_rejected" and d.get("candidate", {}).get("tag") in {
            "C_Unhold",
            "C_Complete",
            "x_DoorClosed",
        }:
            gates = ", ".join(f"{g.event}:{g.detail}" for g in d.get("gates", ()))
            print(f"[scan {event.scan}] REJECTED: {d['candidate']} [{gates}]", flush=True)
        elif k == "zoom":
            print(
                f"[scan {event.scan}] zoom: {d.get('reason')} channel={d.get('channel_tag')}",
                flush=True,
            )
        elif k == "zoom_accepted":
            print(f"[scan {event.scan}] zoom_accepted", flush=True)
        elif k == "zoom_rejected":
            gates = ", ".join(f"{g.event}:{g.detail}" for g in d.get("gates", ()))
            print(f"[scan {event.scan}] zoom_rejected  [{gates}]", flush=True)
        elif k == "letrun_ejection":
            print(f"[scan {event.scan}] letrun_ejection: {d}", flush=True)
        elif k in (
            "provisional_started",
            "provisional_promoted",
            "provisional_regressed",
            "provisional_expired",
        ):
            print(f"[scan {event.scan}] {k}: {d}", flush=True)
        elif k == "trend_regression":
            inv = d.get("investigation") or {}
            confirmed = tuple(h.get("detail") for h in inv.get("confirmed_detail", ()))
            print(
                f"[scan {event.scan}] trend_regression: trend {d.get('from_trend')}->"
                f"{d.get('to_trend')} channels={d.get('channel_transitions')} "
                f"confirmed={confirmed}",
                flush=True,
            )
        elif k == "skiff":
            print(f"[scan {event.scan}] skiff: {d.get('reason', d)}", flush=True)
        elif k == "stuck":
            print(
                f"[scan {event.scan}] STUCK: {d.get('reason')} "
                f"(distance={d.get('distance')}, terminal={d.get('terminal')})",
                flush=True,
            )
        elif k == "finished":
            finished = d
            print(
                f"[scan {event.scan}] FINISHED reached={d['reached']} "
                f"reason={d['reason']!r} steps={len(d['steps'])}",
                flush=True,
            )
            break
    dt = time.perf_counter() - t0
    print(f"\nwall={dt:.1f}s", flush=True)
    if finished is not None:
        work = finished.get("work")
        if work is not None:
            for t in (
                "S_StateCurrent",
                "Internal__Step",
                "S_DryerTemp_F",
                "S_HeatAtTemp_tmr_Acc",
                "S_HeatAtTemp_tmr_Done",
            ):
                print(f"  final {t} = {work.state.tags.get(t)!r}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
