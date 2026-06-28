"""Why is the rotate-watchdog ejection classified zoom_accepted, not LETRUN-EJECTION?

Drives to Execute(6) with the rotate sensor parked False (reusing the focused
driver), then runs pilot_events and reads the terminal-letrun outcome straight
from the event stream — no monkeypatching.  The ``zoom_accepted`` payload now
carries ``observe_label`` / ``zoom_governing_tag`` / ``ejected``, and a
``letrun_ejection`` event reports whether the ejection was investigated and, if
not, why (``reason``).

The LETRUN-EJECTION branch in progress.py needs all three of:
    observe_label == "letrun"  AND  outcome == AMBIENT_DRIFT  AND  zoom_governing_tag is not None
The event stream now shows which of those hold and whether investigation ran.
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


def main() -> int:
    plc = PLC(logic)
    state = _drive_to_execute(plc)
    print(f"reached S_StateCurrent={state} at scan {plc.state.scan_id}  "
          f"Rotate_CurStep={plc.state.tags.get('Rotate_CurStep')}")
    if state != 6:
        print("!! did not reach Execute")
        return 1

    target = plc._known_tags_by_name["y_BurnerLoop"]
    max_scans = int(os.environ.get("PILOT_MAX_SCANS", "6000"))
    print("\n--- PILOT from Execute toward y_BurnerLoop ---")
    for event in pilot_events(plc, target, choice=1, max_scans=max_scans):
        d = event.data
        if event.kind == "zoom_accepted":
            flag = "  EJECTED" if d.get("ejected") else ""
            print(f"[scan {event.scan}] zoom_accepted "
                  f"label={d.get('observe_label')!r} outcome={d.get('outcome')!r} "
                  f"gov={d.get('zoom_governing_tag')!r}->{d.get('zoom_target_value')!r}{flag}")
        elif event.kind == "letrun_ejection":
            print(f"[scan {event.scan}] letrun_ejection  "
                  f"{d.get('governing_tag')}: {d.get('from_value')!r} -> {d.get('to_value')!r}  "
                  f"investigated={d.get('investigated')}  reason={d.get('reason')!r}  "
                  f"span={d.get('coast_span')}")
        elif event.kind == "trend_regression":
            inv = d.get("investigation", {})
            print(f"[scan {event.scan}] trend_regression  hyps={inv.get('hypotheses')} "
                  f"confirmed={inv.get('confirmed')} rejected={inv.get('rejected')}")
        elif event.kind == "zoom":
            print(f"[scan {event.scan}] zoom: {d.get('reason', '')}")
        elif event.kind == "finished":
            print(f"[scan {event.scan}] finished reached={d['reached']} reason={d['reason']}")
            break
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
