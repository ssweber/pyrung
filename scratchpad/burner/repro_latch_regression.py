"""Reproduce the door/lint latch-exposure investigation acceptance.

Drives the burner with pilot_events and stops at the FIRST trend_regression
that carries an investigation payload, printing each hypothesis and whether it
was confirmed/rejected.  Used to A/B 6884b28 (pre bounded-replay) vs HEAD.
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

from main import logic  # noqa: E402

from pyrung import PLC  # noqa: E402
from pyrung.core.analysis.pilot import pilot_events  # noqa: E402


def main() -> int:
    plc = PLC(logic)
    plc.step()
    target = plc._known_tags_by_name["y_BurnerLoop"]
    max_scans = int(os.environ.get("PILOT_MAX_SCANS", "100000"))

    for event in pilot_events(plc, target, choice=1, max_scans=max_scans):
        if event.kind != "trend_regression":
            continue
        inv = event.data.get("investigation", {})
        if not inv:
            continue
        print("=" * 72)
        print(f"trend_regression @ scan {event.scan}")
        print(f"  from_trend={event.data['from_trend']} to_trend={event.data['to_trend']}")
        print(f"  hypotheses={inv.get('hypotheses')} "
              f"confirmed={inv.get('confirmed')} rejected={inv.get('rejected')}")
        # Reconstruct which holds were confirmed: confirmed_holds are installed,
        # but the payload only gives counts.  Print every latch-exposure hyp so
        # we can see them by kind/detail.
        for h in inv.get("hypothesis_detail", ()):
            if h["kind"] != "latch-exposure":
                continue
            holds_str = ", ".join(f"{t}={v!r}" for t, v in h["holds"])
            print(f"  [latch-exposure] {holds_str}")
            print(f"      {h.get('detail', '')}")
        print(f"  CONFIRMED COUNT={inv.get('confirmed')}  (door/lint accepted iff >0)")
        return 0
    print("no investigated trend_regression encountered")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
