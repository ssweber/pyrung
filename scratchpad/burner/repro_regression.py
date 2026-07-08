"""Minimal regression probe: does how(y_BurnerLoop) still reach, and what are
the first-iteration trace candidates?  Prints a compact signal for bisecting."""

from __future__ import annotations

import os
import sys
from pathlib import Path

CLICK_PROJECT = Path(
    os.environ.get(
        "PYRUNG_CLICK_PROJECT",
        r"C:\Users\Sam\AppData\Local\Temp\CLICK (0009051C)\pyrung_project",
    )
)
sys.path.insert(0, str(CLICK_PROJECT))

from main import logic  # noqa: E402

from pyrung import PLC  # noqa: E402
from pyrung.core.analysis.pilot import pilot_events  # noqa: E402


def main() -> int:
    plc = PLC(logic)
    plc.step()
    target = plc._known_tags_by_name["y_BurnerLoop"]
    max_scans = int(os.environ.get("PILOT_MAX_SCANS", "20000"))

    first_candidates_printed = False
    for event in pilot_events(plc, target, max_scans=max_scans):
        d = event.data
        if event.kind == "started":
            route = d.get("route")
            print(f"[STARTED] route={getattr(route, 'label', None)}")
            print(f"  blocked_route_actions={list(d.get('blocked_route_actions', ()))}")
            roles = d.get("pipeline_roles", ())
            print(f"  pipeline_roles: {len(roles)}")
            for r in roles:
                print(f"    channel={r.channel_tag} request={sorted(r.request_tags)}")
        if event.kind == "candidates_built" and not first_candidates_printed:
            first_candidates_printed = True
            cands = d["candidates"]
            print(f"[scan {event.scan}] candidates_built ({len(cands)}):")
            print(f"  stuck_reason={d.get('stuck_reason')}")
            print(f"  wait_prescribed={d.get('wait_prescribed')} wait_reason={d.get('wait_reason')}")
            print(f"  route_candidates={list(d.get('route_candidates', ()))}")
            rp = d.get("route_plan")
            if rp:
                print(f"  route_plan.needed={rp.get('needed')} channel={rp.get('channel_tag')} "
                      f"target_value={rp.get('target_value')!r}")
                for step in rp.get("path", ()):
                    print(f"    step {step['from']!r}->{step['to']!r} action={step.get('action')}")
            else:
                print("  route_plan=None")
            print("  trace_action_details:")
            for det in d.get("trace_action_details", ()):
                print(f"    {det.pair}  prov={det.provenance}  wake={det.wake}")
            print("  candidates:")
            for c in cands:
                print(f"    {c.get('pair')}  route={c.get('route_prescribed')} "
                      f"infl={c.get('influence_prescribed')}  via={c.get('provenance')}  "
                      f"wake={c.get('wake')}")
        if event.kind == "candidate_accepted":
            print(f"[scan {event.scan}] ACCEPTED: {d['candidate'].get('pair')}")
        if event.kind == "finished":
            print(f"FINISHED reached={d['reached']} reason={d['reason']!r} "
                  f"steps={len(d['steps'])}")
            break
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
