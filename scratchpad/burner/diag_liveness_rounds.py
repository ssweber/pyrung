"""Trace the round-by-round liveness investigation.

Drives to Execute(6) with the rotate sensor parked False, then runs pilot_events
and prints, for each trend_regression, the liveness hypotheses and whether each
was CONFIRMED or REJECTED — plus how `forced_holds` for x_RotateSensor evolves
across rounds.  This shows whether the one-sided {True} hold is rejected for
still ejecting (the step-3 problem) and whether a second polarity ever
accumulates (the step-2 problem).
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

CLICK_PROJECT = Path(
    os.environ.get(
        "PYRUNG_CLICK_PROJECT",
        r"C:\Users\ssweb\AppData\Local\Temp\CLICK (00010A00)\pyrung_project",
    )
)
sys.path.insert(0, str(CLICK_PROJECT))

from main import logic  # type: ignore[import-not-found]  # noqa: E402

from pyrung import PLC  # noqa: E402
from pyrung.core.analysis.pilot import pilot_events  # noqa: E402

from pilot_rotate_liveness import _drive_to_execute  # noqa: E402

SENSOR = "x_RotateSensor"


def _fmt_holds(holds) -> str:
    parts = []
    for t, v in holds:
        if t != SENSOR:
            continue
        rules = getattr(v, "rules", None)
        if rules is not None:
            pols = [r.value for r in rules]
            parts.append(f"{t}=osc{pols}")
        else:
            parts.append(f"{t}={v!r}")
    return ", ".join(parts) if parts else "(no sensor hold)"


def main() -> int:
    plc = PLC(logic)
    state = _drive_to_execute(plc)
    print(f"reached S_StateCurrent={state} at scan {plc.state.scan_id}")
    if state != 6:
        return 1

    target = plc._known_tags_by_name["y_BurnerLoop"]
    max_scans = int(os.environ.get("PILOT_MAX_SCANS", "8000"))
    rounds = 0
    for event in pilot_events(plc, target, choice=1, max_scans=max_scans):
        d = event.data
        if event.kind == "trend_regression":
            inv = d.get("investigation", {})
            rounds += 1
            print(f"\n=== regression #{rounds} @ scan {event.scan}  "
                  f"hyps={inv.get('hypotheses')} confirmed={inv.get('confirmed')} "
                  f"rejected={inv.get('rejected')} ===")
            print(f"  forced_holds[sensor]: {_fmt_holds(d.get('forced_holds', {}).items())}")
            for label, key in (("CONFIRMED", "confirmed_detail"), ("REJECTED", "rejected_detail")):
                for h in inv.get(key, ()):
                    if h["kind"] != "liveness":
                        continue
                    print(f"  {label} liveness: {_fmt_holds(h['holds'])}  [{h['detail']}]")
            if rounds >= 6:
                print("\n(stopping after 6 rounds)")
                break
        elif event.kind == "letrun_ejection":
            print(f"[scan {event.scan}] letrun_ejection {d.get('governing_tag')}: "
                  f"{d.get('from_value')!r}->{d.get('to_value')!r} investigated={d.get('investigated')}")
        elif event.kind == "finished":
            print(f"\n[scan {event.scan}] finished reached={d['reached']} reason={d['reason']}")
            break
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
