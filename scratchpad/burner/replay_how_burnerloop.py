"""Double-check the how() plan for y_BurnerLoop by replaying it concretely.

how() returned this plan (via=S_ProductionMode):

  Step 1: C_Clear=True, C_ProductionMode=True                 (3 scans)
  Step 2: C_Reset=True                                        (3 scans)
  Step 3: C_Start=True                                        (3 scans)
  Step 4: (wait)                                              (800 scans)
  Step 5: C_UnitModeChgRequest=True, C_P1_OperatingTempF=120,
          S_DryerTemp_F=-1, x_BlowerFB/x_DoorClosed/
          x_LintDoorClosed/x_RotateFB=True                    (1199 scans)

The suspicious bit: C_ProductionMode is *selected* at step 1, but the mode-change
REQUEST (C_UnitModeChgRequest) only fires at step 5 — "at the last second," after
Clear/Reset/Start already ran in whatever mode the unit was in.  This replays the
plan independently (no how() internals) and watches:
  - WHEN S_UnitModeCurrent actually flips to Production (1)
  - whether y_BurnerLoop truly reaches True
  - whether that late C_UnitModeChgRequest is load-bearing or spurious
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

from pyrung import PLC  # noqa: E402
from main import logic  # noqa: E402

# The exact how() plan, as (forces-to-apply-this-step, scans).  Forces persist
# across steps until explicitly dropped (none are dropped here) — matching
# to_commands() semantics.
PLAN: list[tuple[dict[str, object], int]] = [
    ({"C_Clear": True, "C_ProductionMode": True}, 3),
    ({"C_Reset": True}, 3),
    ({"C_Start": True}, 3),
    ({}, 800),
    (
        {
            "C_UnitModeChgRequest": True,
            "C_P1_OperatingTempF": 120,
            "S_DryerTemp_F": -1,
            "x_BlowerFB": True,
            "x_DoorClosed": True,
            "x_LintDoorClosed": True,
            "x_RotateFB": True,
        },
        1199,
    ),
]

WATCH = (
    "S_UnitModeCurrent",
    "S_ManualMode",
    "S_ProductionMode",
    "C_UnitModeChgRequest",
    "C_ProductionMode",
    "S_StateCurrent",
    "Heat_CurStep",
    "o_BurnerLoop",
    "y_BurnerLoop",
)


def snap(plc: PLC) -> dict[str, object]:
    t = plc.state.tags
    return {k: t.get(k) for k in WATCH}


def line(plc: PLC, label: str) -> None:
    s = snap(plc)
    body = "  ".join(f"{k}={s[k]!r}" for k in WATCH)
    print(f"[scan {plc.state.scan_id:04d}] {label}\n    {body}")


def run_plan(plan, *, label: str, animate_rotate: bool = True) -> tuple[bool, int | None]:
    """Replay a plan; return (reached, scan_of_mode_flip_to_production)."""
    print("\n" + "=" * 78)
    print(label)
    print("=" * 78)
    plc = PLC(logic)
    plc.step()  # the same warm-up scan how()/the sample take before forking
    line(plc, "cold (after warm-up scan)")

    mode_flip_scan: int | None = None
    hit_scan: int | None = None
    prev_mode = plc.state.tags.get("S_UnitModeCurrent")

    for i, (action, scans) in enumerate(plan, 1):
        for name, value in action.items():
            plc.force(name, value)
        if action:
            line(plc, f"step {i}: forced {', '.join(sorted(action))}")
        else:
            line(plc, f"step {i}: (wait)")
        for _ in range(scans):
            if animate_rotate:
                plc.force("x_RotateSensor", (plc.state.scan_id // 50) % 2 == 0)
            plc.step()
            mode = plc.state.tags.get("S_UnitModeCurrent")
            if mode != prev_mode:
                print(
                    f"    -> S_UnitModeCurrent {prev_mode!r} -> {mode!r} "
                    f"at scan {plc.state.scan_id}"
                )
                if mode_flip_scan is None and mode == 1:
                    mode_flip_scan = plc.state.scan_id
                prev_mode = mode
            if plc.state.tags.get("y_BurnerLoop") is True and hit_scan is None:
                hit_scan = plc.state.scan_id
                line(plc, ">>> y_BurnerLoop HIT")
        line(plc, f"after step {i} ({scans} scans)")
        if hit_scan is not None:
            break

    reached = plc.state.tags.get("y_BurnerLoop") is True
    print(
        f"\n  RESULT: reached={reached}  "
        f"y_BurnerLoop first True @ scan={hit_scan}  "
        f"mode->Production @ scan={mode_flip_scan}"
    )
    return reached, mode_flip_scan


def main() -> int:
    print(f"CLICK_PROJECT={CLICK_PROJECT}")

    # 1) Faithful replay of the how() plan.
    reached, flip = run_plan(PLAN, label="A) how() plan verbatim")

    # 2) Drop the late C_UnitModeChgRequest from step 5 — is it load-bearing?
    plan_no_req = [dict(a) for a, _ in PLAN]
    plan_no_req[4].pop("C_UnitModeChgRequest", None)
    plan_b = list(zip(plan_no_req, [s for _, s in PLAN]))
    reached_b, flip_b = run_plan(
        plan_b, label="B) same plan, but WITHOUT the step-5 C_UnitModeChgRequest"
    )

    print("\n" + "#" * 78)
    print("SUMMARY")
    print("#" * 78)
    print(f"A) verbatim plan          : reached={reached}  mode->Prod @ {flip}")
    print(f"B) minus late ChgRequest  : reached={reached_b}  mode->Prod @ {flip_b}")
    if reached and not reached_b:
        print("\n=> The late C_UnitModeChgRequest IS load-bearing: dropping it breaks the plan.")
    elif reached and reached_b:
        print("\n=> The late C_UnitModeChgRequest is NOT needed here (mode flips without it).")
    elif not reached:
        print("\n=> The verbatim plan did NOT reproduce y_BurnerLoop — investigate.")
    return 0 if reached else 1


if __name__ == "__main__":
    raise SystemExit(main())
