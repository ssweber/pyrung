"""Validate cycle_fold_until on the REAL burner Execute coast (read-only).

Drives two identical PLCs to Execute(6) with the rotate sensor parked, installs
the same period-2 oscillation on both, then:
  - reference: scan-by-scan run_until(y_BurnerLoop, fold=False)
  - cyclefold: cycle_fold_until(y_BurnerLoop)
and checks the landings are bit-equal (tags + scan_id) while cyclefold uses a
tiny fraction of the real scans.

Touches NO source files — pure read-only exercise of the new standalone module.
"""

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
from pyrung.core.analysis.pilot.cyclefold import cycle_fold_until  # noqa: E402

PERMISSIVES = {
    "x_DoorClosed": True,
    "x_LintDoorClosed": True,
    "x_BlowerFB": True,
    "x_RotateFB": True,
    "x_SailRelay": True,
}


def drive_to_execute(plc: PLC, budget: int = 4000) -> int:
    for name, value in {**PERMISSIVES, "x_RotateSensor": False}.items():
        plc.force(name, value)
    plc.step()
    plc.patch({"C_ProductionMode": True, "C_UnitModeChgRequest": True})
    plc.step(); plc.step()
    for name in ("C_Clear", "C_Reset", "C_Start"):
        plc.patch({name: True})
        plc.step()
        for _ in range(4):
            plc.step()
    for _ in range(budget):
        plc.force("x_RotateSensor", False)
        plc.step()
        if plc.state.tags.get("S_StateCurrent") == 6:
            break
    return plc.state.tags.get("S_StateCurrent")


def install_oscillator(plc: PLC, tag: str = "x_RotateSensor"):
    plc.unforce(tag)  # release the parked force so the oscillation can drive it

    def _act(s) -> None:
        plc.patch({tag: not s.tags.get(tag, False)})

    return plc.when(lambda s: True).do(_act)


def _done(s) -> bool:
    return s.tags.get("y_BurnerLoop") is True


_RTC_MARKERS = ("PLCDT", "FirstScan", "StopReason_Time", "hhmmss")


def _is_rtc(name: str) -> bool:
    return any(m in name for m in _RTC_MARKERS)


def sig(tags: dict, ignore: frozenset[str] = frozenset()) -> dict:
    """Significant tags: non-default, not fold-excluded, not nondeterministic RTC."""
    return {
        k: v
        for k, v in tags.items()
        if v not in (0, False, None) and k not in ignore and not _is_rtc(k)
    }


def main() -> int:
    ref = PLC(logic)
    if drive_to_execute(ref) != 6:
        print("!! reference did not reach Execute")
        return 1
    cf = PLC(logic)
    if drive_to_execute(cf) != 6:
        print("!! cyclefold did not reach Execute")
        return 1

    ctx = ref._ensure_fold_context()
    ignore = ctx.frozen_writes | ctx.churn_excluded | ctx.profile_fb_names

    # Sanity: both started equivalent at Execute (significant tags).
    if sig(ref.state.tags, ignore) != sig(cf.state.tags, ignore):
        sr, sc = sig(ref.state.tags, ignore), sig(cf.state.tags, ignore)
        diff = {k: (sr.get(k), sc.get(k)) for k in set(sr) | set(sc) if sr.get(k) != sc.get(k)}
        print(f"!! drives diverged before coast ({len(diff)} tags): "
              f"{dict(list(diff.items())[:15])}")
        return 1
    start_scan = ref.state.scan_id
    print(f"both at Execute, scan {start_scan}")

    install_oscillator(ref)
    install_oscillator(cf)

    ref.run_until(_done, max_cycles=20000, fold=False)
    ref_scans = ref.state.scan_id - start_scan
    print(f"reference: y_BurnerLoop={ref.state.tags.get('y_BurnerLoop')} "
          f"in {ref_scans} scan-by-scan scans (Heat_CurStep={ref.state.tags.get('Heat_CurStep')})")

    stats: dict[str, int] = {}
    reached = cycle_fold_until(cf, _done, budget=20000, stats=stats)
    cf_scans = stats.get("real_scans", -1)
    print(f"cyclefold: reached={reached} folds={stats.get('folds')} "
          f"real_scans={cf_scans} (Heat_CurStep={cf.state.tags.get('Heat_CurStep')})")

    # Bit-equality of the landing (significant tags — sparse-store normalized).
    sr, sc = sig(ref.state.tags, ignore), sig(cf.state.tags, ignore)
    if sr == sc:
        print("[OK] tags BIT-EQUAL to scan-by-scan")
    else:
        diff = {k: (sr.get(k), sc.get(k)) for k in set(sr) | set(sc) if sr.get(k) != sc.get(k)}
        print(f"[X] tags DIFFER ({len(diff)}): {dict(list(diff.items())[:20])}")
    print(f"scan_id: ref={ref.state.scan_id} cf={cf.state.scan_id} "
          f"{'[OK] equal' if ref.state.scan_id == cf.state.scan_id else '[X] DIFFER'}")
    if cf_scans > 0 and ref_scans > 0:
        print(f"speedup: {ref_scans / cf_scans:.1f}x fewer real scans")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
