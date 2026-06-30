"""Probe 3 — does the recorded PATH need to carry the reactive hold?

Today a terminal let-run commits ``_Step(action={}, scan_before, scan_after)`` —
empty action, just a scan span (pilot.py _commit_step, pulse_actions=() for a
let-run).  The oscillation that makes that span succeed lives ONLY in
``state.forced_holds`` as a ``ConditionalHold`` — a pilot-internal object, not a
runner primitive the step records.

So the recorded *path* (the steps) is not self-describing: replaying the steps
alone (patch action; run span) cannot reproduce the let-run, because the step
carries no oscillation.  This probe demonstrates that and shows the fix: record
the reactive hold ON the step as a runner-native ``when(guard).do(patch)``
registration, so path replay reproduces the solve with pyrung primitives only —
no pilot internals.

Three replays of the SAME Execute(6) -> y_BurnerLoop let-run span:

  NAIVE      step.action only (today's _Step) — run the span, no oscillation.
             EXPECT FAIL (rotate watchdog trips, S_StateCurrent -> 8).
  PRIMITIVE  step carries reactive_holds; replay registers when().do() from the
             step + run_until(fold=True).  EXPECT SUCCESS, == the pilot solve.
  INTERNAL   today's pilot replay: reconstruct ConditionalHold from forced_holds,
             animate via _coast_holding_state.  Reference oracle.

Run:  uv run python scratchpad/burner/probe_path_replay.py
"""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

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

# The confirmed round-2 oscillation (probe 1).
DOUBLE = ConditionalHold(
    rules=(
        _HoldRule(value=False, guard_tag=SENSOR, guard_op="ne", guard_value=False),
        _HoldRule(value=True, guard_tag=SENSOR, guard_op="ne", guard_value=True),
    )
)


# What a self-describing path step would look like: action + reactive holds.
@dataclass
class RecordedStep:
    action: dict[str, Any] = field(default_factory=dict)
    scans: int = 0
    # NEW: the reactive holds animating during this step, as runner primitives.
    reactive_holds: dict[str, ConditionalHold] = field(default_factory=dict)


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


def _reached(s) -> bool:
    return _values_match(s.tags.get(TARGET_TAG), TARGET_VAL)


def _hit(plc: PLC) -> bool:
    return _values_match(plc.state.tags.get(TARGET_TAG), TARGET_VAL)


def _end(plc: PLC) -> str:
    return (
        f"reached={_hit(plc)}  scan={plc.state.scan_id} "
        f"S_StateCurrent={plc.state.tags.get('S_StateCurrent')} "
        f"Rotate_Error={plc.state.tags.get('Rotate_Error')} "
        f"{TARGET_TAG}={plc.state.tags.get(TARGET_TAG)}"
    )


# NAIVE: replay today's _Step (action only) — no oscillation recorded.
def replay_naive(plc: PLC, step: RecordedStep, budget: int) -> None:
    if step.action:
        plc.patch(step.action)
    plc.run_until(_reached, max_cycles=budget, fold=True)


# PRIMITIVE: the step carries reactive_holds; replay them as when().do()+patch.
def replay_primitive(plc: PLC, step: RecordedStep, budget: int) -> None:
    if step.action:
        plc.patch(step.action)
    handles = []
    for tag, ch in step.reactive_holds.items():
        def make_act(tag, ch):
            return lambda s: (plc.patch({tag: ch.value_for(s.tags)[1]}) if ch.value_for(s.tags)[0] else None)

        def make_guard(ch):
            return lambda s: ch.value_for(s.tags)[0]

        handles.append(plc.when(make_guard(ch)).do(make_act(tag, ch)))
    for tag, ch in step.reactive_holds.items():
        active, value = ch.value_for(plc.state.tags)
        if active:
            plc.patch({tag: value})
    try:
        plc.run_until(_reached, max_cycles=budget, fold=True)
    finally:
        for h in handles:
            h.remove()


def main() -> int:
    plc = PLC(logic)
    state = _drive_to_execute(plc)
    print(f"reached S_StateCurrent={state} at scan {plc.state.scan_id}")
    if state != 6:
        print("!! did not reach Execute; aborting")
        return 1
    base = plc.fork()
    budget = 6000

    # the recorded let-run step, two recordings of it:
    step_today = RecordedStep(action={}, scans=0)  # what _Step records now
    step_fixed = RecordedStep(action={}, scans=0, reactive_holds={SENSOR: DOUBLE})

    fn = base.fork(); _force_permissives(fn)
    replay_naive(fn, step_today, budget)

    fp = base.fork(); _force_permissives(fp)
    replay_primitive(fp, step_fixed, budget)

    fi = base.fork(); _force_permissives(fi)
    _coast_holding_state(fi, TARGET_TAG, TARGET_VAL, ROLE_TAGS, conditional={SENSOR: DOUBLE}, budget=budget)

    print(f"\n  NAIVE  (step.action only, no hold) : {_end(fn)}")
    print(f"  PRIMITIVE (hold recorded on step)  : {_end(fp)}")
    print(f"  INTERNAL (forced_holds oracle)     : {_end(fi)}")

    print("\n========== VERDICT ==========")
    print(f"  naive reproduces the solve?     {_hit(fn)}  (expect False — gap)")
    print(f"  primitive reproduces the solve? {_hit(fp)}  (expect True)")
    match = (
        _hit(fp) == _hit(fi)
        and fp.state.tags.get("S_StateCurrent") == fi.state.tags.get("S_StateCurrent")
        and fp.state.scan_id == fi.state.scan_id
    )
    print(f"  primitive == internal oracle?   {match}")
    print(
        "\n  => the PATH must record the reactive hold on the let-run step; "
        "recording it as when().do() makes the path replay with primitives only."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
