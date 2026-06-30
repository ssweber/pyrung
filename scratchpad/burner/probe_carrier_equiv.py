"""Probe 1 — carrier-equivalence for the when().do() hold swap.

Question: does a runner-native ``when(guard).do(patch)`` oscillator reproduce,
scan-for-scan, the bespoke force-based ``_coast_holding_state`` conditional loop?

We DON'T exercise pilot's hypothesis generation here — we hand-build the exact
``ConditionalHold`` the investigation ends up with and test only the CARRIER:

  * single-polarity  (round 1)  rules=(drive True while != True,)
  * two-polarity     (round 2)  rules=(drive False while != False,
                                       drive True  while != True)

For each hold we fork the same pre-positioned Execute(6) state three ways:

  A  force-coast      — instrumented mirror of _ops._coast_holding_state (the
                        conditional branch), scan-by-scan, force()-based.
  B  reactive-coast   — candidate: plc.when(value_for active).do(patch), fold OFF.
  C  reactive-coast   — same, fold ON  (the fold-safety / fold-resume win).

A vs B should match scan-for-scan (the swap is behavior-preserving).  C is the
end-state under folding.  We also call the REAL _coast_holding_state once to
confirm the instrumented force mirror is faithful.

Run:  uv run python scratchpad/burner/probe_carrier_equiv.py
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
from pyrung.core.analysis.pilot._ops import (  # noqa: E402
    ConditionalHold,
    _HoldRule,
    _coast_holding_state,
)
from pyrung.core.analysis.sp_values import _values_match  # noqa: E402

SENSOR = "x_RotateSensor"
TARGET_TAG = "y_BurnerLoop"
TARGET_VAL = True
ROLE_TAGS = ("S_StateCurrent",)

PERMISSIVES = {
    "x_DoorClosed": True,
    "x_LintDoorClosed": True,
    "x_BlowerFB": True,
    "x_RotateFB": True,
    "x_SailRelay": True,
}

TRACE_TAGS = (
    SENSOR,
    "i_RotateSensor",
    "Rotate_CurStep",
    "Rotate_Error",
    "Rotate_SensorOnWD_tmr.Done",
    "Rotate_SensorOnWD_tmr.Acc",
    "Rotate_SensorOffWD_tmr.Done",
    "Rotate_SensorOffWD_tmr.Acc",
    "Heat_CurStep",
    "S_StateCurrent",
    TARGET_TAG,
)

# Holds under test ----------------------------------------------------------
SINGLE = ConditionalHold(rules=(_HoldRule(value=True, guard_tag=SENSOR, guard_op="ne", guard_value=True),))
DOUBLE = ConditionalHold(
    rules=(
        _HoldRule(value=False, guard_tag=SENSOR, guard_op="ne", guard_value=False),
        _HoldRule(value=True, guard_tag=SENSOR, guard_op="ne", guard_value=True),
    )
)


def _drive_to_execute(plc: PLC, budget: int = 4000) -> int:
    for name, value in {**PERMISSIVES, SENSOR: False}.items():
        plc.force(name, value)
    plc.step()
    plc.patch({"C_ProductionMode": True, "C_UnitModeChgRequest": True})
    plc.step()
    plc.step()
    for name in ("C_Clear", "C_Reset", "C_Start"):
        plc.patch({name: True})
        plc.step()
        for _ in range(4):
            plc.step()
    for _ in range(budget):
        plc.force(SENSOR, False)
        plc.step()
        if plc.state.tags.get("S_StateCurrent") == 6:
            break
    return plc.state.tags.get("S_StateCurrent")


def _row(state) -> tuple:
    return tuple(state.tags.get(t) for t in TRACE_TAGS)


def _force_permissives(fork: PLC) -> None:
    for name, value in PERMISSIVES.items():
        fork.force(name, value)


# A — instrumented force-coast (faithful mirror of _ops._coast_holding_state) -
def force_coast(plc, *, conditional, budget, trace):
    start = {t: plc.state.tags.get(t) for t in ROLE_TAGS}

    def reached():
        return _values_match(plc.state.tags.get(TARGET_TAG), TARGET_VAL)

    def ejected():
        return any(not _values_match(plc.state.tags.get(t), start[t]) for t in ROLE_TAGS)

    def drive(s):
        snap = dict(s.tags)
        for tag, ch in conditional.items():
            active, value = ch.value_for(snap)
            if active:
                plc.force(tag, value)

    drive(plc.state)
    for _ in range(budget):
        plc.step()
        drive(plc.state)
        trace.append(_row(plc.state))
        if reached() or ejected():
            break
    return reached()


# B / C — candidate reactive coast (when().do() + patch) --------------------
def reactive_coast(plc, *, conditional, budget, fold, trace):
    start = {t: plc.state.tags.get(t) for t in ROLE_TAGS}

    def reached(s):
        return _values_match(s.tags.get(TARGET_TAG), TARGET_VAL)

    def ejected(s):
        return any(not _values_match(s.tags.get(t), start[t]) for t in ROLE_TAGS)

    handles = []
    for tag, ch in conditional.items():
        def make_act(tag, ch):
            def _act(s):
                active, value = ch.value_for(s.tags)
                if active:
                    plc.patch({tag: value})
            return _act

        def make_guard(ch):
            return lambda s: ch.value_for(s.tags)[0]

        handles.append(plc.when(make_guard(ch)).do(make_act(tag, ch)))

    # mirror the force version's pre-step drive (force()'s first assertion)
    for tag, ch in conditional.items():
        active, value = ch.value_for(plc.state.tags)
        if active:
            plc.patch({tag: value})

    handles.append(plc.when(lambda s: True).do(lambda s: trace.append(_row(s))))
    handles.append(plc.when(ejected).pause())
    try:
        plc.run_until(reached, max_cycles=budget, fold=fold)
    finally:
        for h in handles:
            h.remove()
    return _values_match(plc.state.tags.get(TARGET_TAG), TARGET_VAL)


def _end(state) -> str:
    return (
        f"scan={state.scan_id} t={state.timestamp:.2f}  "
        f"S_StateCurrent={state.tags.get('S_StateCurrent')} "
        f"Rotate_Error={state.tags.get('Rotate_Error')} "
        f"Heat_CurStep={state.tags.get('Heat_CurStep')} "
        f"{TARGET_TAG}={state.tags.get(TARGET_TAG)}"
    )


def _compare(label, hold, base: PLC, budget):
    print(f"\n========== {label} ==========")

    # ground-truth: real _coast_holding_state (force path)
    real = base.fork()
    _force_permissives(real)
    real_reached = _coast_holding_state(
        real, TARGET_TAG, TARGET_VAL, ROLE_TAGS, conditional={SENSOR: hold}, budget=budget
    )

    fa = base.fork()
    _force_permissives(fa)
    ta: list = []
    ra = force_coast(fa, conditional={SENSOR: hold}, budget=budget, trace=ta)

    fb = base.fork()
    _force_permissives(fb)
    tb: list = []
    rb = reactive_coast(fb, conditional={SENSOR: hold}, budget=budget, fold=False, trace=tb)

    fc = base.fork()
    _force_permissives(fc)
    tc: list = []
    rc = reactive_coast(fc, conditional={SENSOR: hold}, budget=budget, fold=True, trace=tc)

    print(f"  REAL force coast : reached={real_reached}  {_end(real.state)}")
    print(f"  A force (mirror) : reached={ra}  {_end(fa.state)}  rows={len(ta)}")
    print(f"  B reactive nofold: reached={rb}  {_end(fb.state)}  rows={len(tb)}")
    print(f"  C reactive fold  : reached={rc}  {_end(fc.state)}  rows={len(tc)}")

    mirror_ok = _values_match(fa.state.tags.get(TARGET_TAG), real.state.tags.get(TARGET_TAG)) and \
        fa.state.tags.get("S_StateCurrent") == real.state.tags.get("S_StateCurrent")
    print(f"  mirror faithful (A == REAL end-state): {mirror_ok}")

    # per-scan A vs B
    n = min(len(ta), len(tb))
    first_div = next((i for i in range(n) if ta[i] != tb[i]), None)
    if first_div is None and len(ta) == len(tb):
        print(f"  A vs B per-scan: IDENTICAL across all {len(ta)} rows")
    else:
        print(f"  A vs B per-scan: DIVERGE at row {first_div} (len A={len(ta)} B={len(tb)})")
        lo = 0 if first_div is None else max(0, first_div - 2)
        hi = (min(n, lo + 6))
        print(f"    cols: {TRACE_TAGS}")
        for i in range(lo, hi):
            mark = "  <-- first div" if i == first_div else ""
            print(f"    row {i:>4}  A={ta[i]}{mark}")
            print(f"             B={tb[i]}")
    return ra, rb, rc, mirror_ok, (first_div is None and len(ta) == len(tb))


def main() -> int:
    plc = PLC(logic)
    state = _drive_to_execute(plc)
    print(f"reached S_StateCurrent={state} at scan {plc.state.scan_id} (dt={plc._dt})")
    print(f"  Rotate_CurStep={plc.state.tags.get('Rotate_CurStep')} "
          f"Heat_CurStep={plc.state.tags.get('Heat_CurStep')} "
          f"{SENSOR}={plc.state.tags.get(SENSOR)}")
    if state != 6:
        print("!! did not reach Execute; aborting")
        return 1

    base = plc.fork()  # clean Execute(6) snapshot to fork from repeatedly

    _compare("SINGLE polarity (round 1 — expect eject SensorOnWD)", SINGLE, base, budget=3000)
    _compare("TWO polarity (round 2 — expect oscillate -> y_BurnerLoop)", DOUBLE, base, budget=6000)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
