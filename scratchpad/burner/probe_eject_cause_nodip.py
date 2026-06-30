"""Probe 2 — eject-cause identity + no-dip under the reactive carrier.

Probe 1 showed the reactive coast is scan-for-scan identical to force (no-fold)
and that fold lands the single-polarity eject 3 scans later.  Two things the
round-by-round investigation depends on, made explicit here:

  (1) EJECT CAUSE.  build_replay_fn's new-cause acceptance keys on *which* Done
      bit ejected (eject_cause_dones).  The fold path lands 3 scans later — we
      must confirm it ejects on the SAME watchdog (SensorOnWD.Done), not a
      different one, or round-by-round accumulation changes shape under fold.

  (2) NO DIP.  Single-polarity pins the sensor True; if i_RotateSensor ever dips
      False after Rotate_CurStep>=3, SensorOnWD (reset on ~i_RotateSensor) resets
      and never trips.  We prove the longest False-run after enablement is 0 and
      SensorOnWD.Acc climbs monotonically with no reset.

      Folding can't hide a dip: a sensor change is a *visible* item, so any dip
      would break the plateau and be recorded even in the sparse fold trace.

Run:  uv run python scratchpad/burner/probe_eject_cause_nodip.py
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
from pyrung.core.analysis.pilot._ops import ConditionalHold, _HoldRule  # noqa: E402
from pyrung.core.analysis.sp_values import _values_match  # noqa: E402

SENSOR = "x_RotateSensor"
TARGET_TAG = "y_BurnerLoop"
TARGET_VAL = True
ROLE_TAGS = ("S_StateCurrent",)
ON_WD = "Rotate_SensorOnWD_tmr_Done"
OFF_WD = "Rotate_SensorOffWD_tmr_Done"
ON_ACC = "Rotate_SensorOnWD_tmr_Acc"

PERMISSIVES = {
    "x_DoorClosed": True,
    "x_LintDoorClosed": True,
    "x_BlowerFB": True,
    "x_RotateFB": True,
    "x_SailRelay": True,
}

SINGLE = ConditionalHold(rules=(_HoldRule(value=True, guard_tag=SENSOR, guard_op="ne", guard_value=True),))


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


def _force_permissives(fork: PLC) -> None:
    for name, value in PERMISSIVES.items():
        fork.force(name, value)


def reactive_single_coast(plc, *, budget, fold, trace):
    """Single-polarity reactive coast; record (scan, i_RotateSensor, CurStep, OnAcc,
    OnDone, OffDone, Rotate_Error, S_StateCurrent) each recorded scan."""
    ch = SINGLE
    start = {t: plc.state.tags.get(t) for t in ROLE_TAGS}

    def reached(s):
        return _values_match(s.tags.get(TARGET_TAG), TARGET_VAL)

    def ejected(s):
        return any(not _values_match(s.tags.get(t), start[t]) for t in ROLE_TAGS)

    def rec(s):
        trace.append(
            (
                s.scan_id,
                s.tags.get("i_RotateSensor"),
                s.tags.get("Rotate_CurStep"),
                s.tags.get(ON_ACC),
                s.tags.get(ON_WD),
                s.tags.get(OFF_WD),
                s.tags.get("Rotate_Error"),
                s.tags.get("S_StateCurrent"),
            )
        )

    handles = [
        plc.when(lambda s: ch.value_for(s.tags)[0]).do(
            lambda s: plc.patch({SENSOR: ch.value_for(s.tags)[1]})
        ),
    ]
    active, value = ch.value_for(plc.state.tags)
    if active:
        plc.patch({SENSOR: value})
    handles.append(plc.when(lambda s: True).do(rec))
    handles.append(plc.when(ejected).pause())
    try:
        plc.run_until(reached, max_cycles=budget, fold=fold)
    finally:
        for h in handles:
            h.remove()


def _analyze(label, trace):
    last = trace[-1]
    scan, isens, curstep, on_acc, on_done, off_done, err, sstate = last
    eject_cause = []
    if on_done:
        eject_cause.append("SensorOnWD")
    if off_done:
        eject_cause.append("SensorOffWD")

    # longest run of i_RotateSensor==False once Rotate_CurStep>=3 (watchdog-enabled)
    longest_false = 0
    run = 0
    on_acc_resets = 0
    prev_acc = None
    for (_sc, i_s, cs, oacc, *_rest) in trace:
        if cs is not None and cs >= 3:
            if i_s is False:
                run += 1
                longest_false = max(longest_false, run)
            else:
                run = 0
            if prev_acc is not None and isinstance(oacc, (int, float)) and oacc < prev_acc:
                on_acc_resets += 1
            prev_acc = oacc

    print(f"\n--- {label} ---")
    print(f"  rows={len(trace)}  eject scan={scan} S_StateCurrent={sstate} Rotate_Error={err}")
    print(f"  eject cause Done bit(s): {eject_cause or ['<none>']}  (OnAcc={on_acc})")
    print(f"  longest i_RotateSensor False-run while CurStep>=3: {longest_false}  (want 0)")
    print(f"  SensorOnWD.Acc resets while CurStep>=3: {on_acc_resets}  (want 0)")
    return tuple(eject_cause), longest_false, on_acc_resets


def main() -> int:
    plc = PLC(logic)
    state = _drive_to_execute(plc)
    print(f"reached S_StateCurrent={state} at scan {plc.state.scan_id}")
    if state != 6:
        print("!! did not reach Execute; aborting")
        return 1
    base = plc.fork()

    tn: list = []
    fn = base.fork()
    _force_permissives(fn)
    reactive_single_coast(fn, budget=3000, fold=False, trace=tn)

    tf: list = []
    ff = base.fork()
    _force_permissives(ff)
    reactive_single_coast(ff, budget=3000, fold=True, trace=tf)

    cause_n, false_n, resets_n = _analyze("NO-FOLD single-polarity", tn)
    cause_f, false_f, resets_f = _analyze("FOLD single-polarity", tf)

    print("\n========== VERDICT ==========")
    print(f"  same eject cause (no-fold == fold): {cause_n == cause_f}  ({cause_n} vs {cause_f})")
    print(f"  no dip (both False-runs == 0):      {false_n == 0 and false_f == 0}")
    print(f"  no WD reset (both reset-counts ==0):{resets_n == 0 and resets_f == 0}")
    ok = cause_n == cause_f == ("SensorOnWD",) and false_n == false_f == 0 and resets_n == resets_f == 0
    print(f"  ALL GREEN: {ok}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
