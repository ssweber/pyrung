"""Machine-local live check: the free-word relational lever on the fill project.

The gate program in tests/core/analysis/test_pilot_free_word_lever.py is the CI
tier; this script is the live tier (machine-local, not CI): the real fill project
at C:/Users/Sam/AppData/Local/Temp/CLICK (00031CF4)/pyrung_project.

Two checks:

1. The mechanism check — how(pv_LevelHt < calc_levelSvLowerWBand) drives the live
   compare through the snapshot-frozen rewrite: the plan reaches, replay confirms,
   and the rendered plan carries the relational "held ... to satisfy ... (e.g., ...)"
   note.  This is the seam the 2026-07 heuristic-lever change landed.

2. The original invocation — how(fill_solv_nc, avoid=HMI_fill).  Ground truth
   (hand-driveable): HMI_on=True, systemLevel_opt2011=100.0, sv_levelSetPoint=100.0
   -> fill_solv_nc at scan 5.  KNOWN RESIDUAL (deferred, out of the lever seam):
   this still declines naming sv_levelBand, because the trace's same-tag value
   budget (_SAME_TAG_VALUE_BUDGET=1) cuts the fill stepper at fill_stepNumber 4<-3,
   so the pv<lower compare never enters the tree and the relational lever is
   structurally unreachable from that frontier; the 4-node is traced through the
   circular decrement writer (Main R8: step==5 & HMI_resetError -> step-1) instead.
   The loop's own advance (HMI_on pulse) is rejected as lateral (the oneshot
   stepper effect is delayed).  Fixing that is stepper/route territory, not the
   free-word lever.
"""

from __future__ import annotations

import sys

PROJECT = r"C:\Users\Sam\AppData\Local\Temp\CLICK (00031CF4)\pyrung_project"
sys.path.insert(0, PROJECT)

from pyrung import PLC  # noqa: E402


def main() -> int:
    import main as fill_main  # the project's main.py (defines logic)
    import tags as fill_tags

    logic = fill_main.logic

    # --- ground truth (hand-driveable) ---
    gt = PLC(logic)
    gt.patch(
        {
            "HMI_on": True,
            "systemLevel_opt2011": 100.0,
            "sv_levelSetPoint": 100.0,
        }
    )
    reached_at = None
    for _ in range(20):
        gt.step()
        if gt.current_state.tags.get("fill_solv_nc"):
            reached_at = gt.current_state.scan_id
            break
    print(f"ground truth: fill_solv_nc reached at scan {reached_at}")
    assert reached_at is not None, "ground truth hand-drive failed"

    # --- 1. mechanism check: drive the live compare relation ---
    runner = PLC(logic)
    runner.step()
    plan = runner.how(
        fill_tags.pv_LevelHt < fill_tags.calc_levelSvLowerWBand, max_scans=1500
    )
    print(f"\nhow(pv_LevelHt < calc_levelSvLowerWBand): reachable={plan.reachable}")
    assert plan.reachable, plan.reason
    replay = plan.replay()
    rt = replay.current_state.tags
    ok = rt.get("pv_LevelHt") < rt.get("calc_levelSvLowerWBand")
    print(f"replay confirms pv < lower: {ok}")
    assert ok
    print()
    print(str(plan))
    notes = [n for step in plan.journal for n in step.notes]
    assert any("e.g." in n for n in notes), f"no relational note: {plan.journal!r}"
    assert any("pv_LevelHt" in n and "calc_levelSvLowerWBand" in n for n in notes)

    # --- 2. original invocation (known residual: stepper trace cut) ---
    runner2 = PLC(logic)
    plan2 = runner2.how(fill_tags.fill_solv_nc, avoid=fill_tags.HMI_fill)
    print(f"\nhow(fill_solv_nc, avoid=HMI_fill): reachable={plan2.reachable}")
    if plan2.reachable:
        r2 = plan2.replay()
        confirmed = bool(r2.current_state.tags.get("fill_solv_nc"))
        print(f"replay confirms fill_solv_nc: {confirmed}")
        print(str(plan2))
        return 0 if confirmed else 1
    print(f"RESIDUAL (see module docstring): {plan2.reason}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
