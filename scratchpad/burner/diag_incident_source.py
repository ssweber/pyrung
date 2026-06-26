"""Capture what the regression incident is actually built from.

Monkeypatches investigate_deviation to dump the incident's plc identity, scan
window, departures, and whether the door-alarm latch is in changed_tags.
"""
from __future__ import annotations
import os, sys
from pathlib import Path
CLICK_PROJECT = Path(os.environ.get("PYRUNG_CLICK_PROJECT",
    r"C:\Users\Sam\AppData\Local\Temp\CLICK (0009051C)\pyrung_project"))
sys.path.insert(0, str(CLICK_PROJECT))
from main import logic  # noqa: E402
from pyrung import PLC  # noqa: E402
import pyrung.core.analysis.pilot.progress as progress  # noqa: E402
from pyrung.core.analysis.pilot import pilot_events  # noqa: E402

_real = progress.investigate_deviation


def _spy(plc, incident, ctx, replay):
    print("\n=== INCIDENT ===")
    print(f"  plc id={id(plc):x} scan_id={plc.state.scan_id}")
    try:
        h = plc.history
        print(f"  plc history: oldest={h.oldest_scan_id} newest={h.newest_scan_id}")
    except Exception as e:  # noqa: BLE001
        print(f"  plc history: <error {e!r}>")
    print(f"  anchor_scan={incident.anchor_scan} end_scan={incident.end_scan} "
          f"departure_scan={incident.departure_scan}")
    print(f"  changed_tags ({len(incident.changed_tags)}):")
    door = [t for t in incident.changed_tags if "Alm14" in t or "Door" in t]
    print(f"    door-related in changed_tags: {door}")
    print(f"  departures: {[(d.tag, d.value, d.scan) for d in incident.departures]}")
    from pyrung.core.analysis.pilot.investigate import (
        _latch_exposure_hypotheses,
        _upstream_hypotheses,
    )
    aft = incident.after_snap
    print(f"  A_Alm14_Trig after={aft.get('A_Alm14_DoorOpen_Trig')} "
          f"A_Alm15_Trig after={aft.get('A_Alm15_LintOpen_Trig')}")
    print(f"  S_Starting before={incident.before_snap.get('S_Starting')} "
          f"opaque_has_S_Starting={'S_Starting' in getattr(ctx,'opaque_loop',frozenset())}")
    from pyrung.core.analysis.pdg import resolve_rung
    from pyrung.core.instruction.coils import LatchInstruction
    tg = "A_Alm14_DoorOpen_Trig"
    wr = ctx.pdg.writers_of.get(tg, frozenset())
    print(f"  {tg}: writers={sorted(wr)}")
    for ri in wr:
        ro = resolve_rung(ctx.program, ctx.pdg.rung_nodes[ri])
        islatch = ro is not None and any(isinstance(i, LatchInstruction) for i in ro._instructions)
        sp = ro.sp_tree() if ro is not None else None
        tn = set(getattr(sp, "tag_names", ()) or ()) if sp is not None else None
        print(f"    ri={ri} resolved={ro is not None} is_latch={islatch} sp_tags={tn}")
    lx = _latch_exposure_hypotheses(plc, incident, ctx)
    print(f"  latch-exposure hypotheses ({len(lx)}):")
    for h in lx:
        print(f"    {h.kind} holds={h.holds} detail={h.detail!r}")
    # Probe the replay directly for each door hold to see the reached verdict.
    for hold in (("x_DoorClosed", True), ("x_LintDoorClosed", True)):
        out = replay((hold,))
        print(f"  replay({hold}) -> accepted={out.accepted} reason={out.reason!r}")
    out = replay((("x_DoorClosed", True), ("x_LintDoorClosed", True)))
    print(f"  replay(door+lint) -> accepted={out.accepted} reason={out.reason!r}")
    result = _real(plc, incident, ctx, replay)
    print(f"  -> confirmed={[h.holds for h in result.confirmed]} "
          f"rejected={len(result.rejected)}")
    return result


progress.investigate_deviation = _spy


def main():
    plc = PLC(logic)
    plc.step()
    target = plc._known_tags_by_name["y_BurnerLoop"]
    n = 0
    for event in pilot_events(plc, target, choice=1, max_scans=100000):
        if event.kind == "trend_regression":
            n += 1
            if n >= 2:
                break
        if event.kind == "finished":
            break
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
