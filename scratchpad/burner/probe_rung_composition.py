"""Compare manual and synthesized steering at the first Execute watchdog.

Run with:
    uv run python scratchpad/burner/probe_rung_composition.py
"""

from __future__ import annotations

import importlib
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from pyrung.core.analysis.pilot._ops import PilotRung, _set_rungs
from pyrung.core.condition import AllCondition, CompareEq, CompareLt, CompareNe
from tests.fixtures.tumbler import enter_production
from tests.tumbler.bench import Bench


def _execute_world():
    logic = importlib.import_module("tests.fixtures.tumbler").logic
    bench = Bench(logic)
    bench.force_physical()
    bench.step()
    enter_production(bench.plc)
    bench.scan = bench.plc.state.scan_id
    for command in ("Cmd_State_Clear", "Cmd_State_Reset", "Cmd_State_Start"):
        bench.patch({command: True})
        bench.step(5)
    assert bench.step_until(lambda: bench.get("Sts_StateCurrent") == 6, 4_000)
    assert bench.step_until(lambda: bench.get("Internal__Step") == 101, 4_000)
    return bench.plc


def _report(label: str, plc, samples: list[tuple[object, ...]]) -> None:
    tags = plc.state.tags
    print(
        label,
        "scan",
        plc.state.scan_id,
        "state",
        tags.get("Sts_StateCurrent"),
        "error",
        tags.get("Rotate_Error"),
        "sensor",
        tags.get("x_RotateSensor"),
        tags.get("i_RotateSensor"),
        "watchdogs",
        tags.get("Rotate_SensorOnWD_tmr_Acc"),
        tags.get("Rotate_SensorOffWD_tmr_Acc"),
        "heat",
        tags.get("S_HeatAtTemp_tmr_Acc"),
    )
    print("  first samples", samples[:8])
    print("  last samples", samples[-8:])


def _sample(plc) -> tuple[object, ...]:
    tags = plc.state.tags
    return (
        plc.state.scan_id,
        tags.get("x_RotateSensor"),
        tags.get("i_RotateSensor"),
        tags.get("Rotate_SensorOnWD_tmr_Acc"),
        tags.get("Rotate_SensorOffWD_tmr_Acc"),
        tags.get("Rotate_Error"),
        tags.get("Sts_StateCurrent"),
    )


def main() -> None:
    base = _execute_world()

    manual = base.fork()
    manual_samples = []
    for scan in range(1_200):
        manual.force("S_DryerTemp_F", 131)
        manual.force("x_RotateSensor", scan % 2 == 0)
        manual.step()
        if scan < 8 or scan % 100 == 0 or manual.state.tags.get("Rotate_Error"):
            manual_samples.append(_sample(manual))
        if manual.state.tags.get("Sts_StateCurrent") != 6:
            break
    _report("manual-every-scan", manual, manual_samples)

    runged = base.fork()
    tags = runged._known_tags_by_name
    timer_unresolved = CompareLt(
        tags["S_HeatAtTemp_tmr_Acc"],
        tags["Sts_P2_Dry_Tm"],
    )
    watchdog_not_done = CompareEq(tags["Rotate_SensorOffWD_tmr_Done"], False)
    _set_rungs(
        runged,
        (
            PilotRung("S_DryerTemp_F", 131, timer_unresolved),
            PilotRung(
                "x_RotateSensor",
                False,
                AllCondition(
                    watchdog_not_done,
                    CompareNe(tags["x_RotateSensor"], False),
                ),
            ),
            PilotRung(
                "x_RotateSensor",
                True,
                AllCondition(
                    watchdog_not_done,
                    CompareNe(tags["x_RotateSensor"], True),
                ),
            ),
        ),
    )
    rung_samples = []
    for scan in range(1_200):
        runged.step()
        if scan < 8 or scan % 100 == 0 or runged.state.tags.get("Rotate_Error"):
            rung_samples.append(_sample(runged))
        if runged.state.tags.get("Sts_StateCurrent") != 6:
            break
    _report("pilot-rungs", runged, rung_samples)


if __name__ == "__main__":
    main()
