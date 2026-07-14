"""Known-good prefix: cold boot to the burner loop.

Port of ``scratchpad/burner/reconstitute_y_burnerloop_steps.py``.  Drives the
generated tumbler program like a test bench — no how(), no shortcuts:

1. Hold physical permissives/feedback true.
2. Select Production mode.
3. Pulse Clear, Reset, Start.
4. Keep the rotate sensor moving while the SFCs initialize.
5. Wait until Heat reaches step 3 and turns on the burner output.

The pre-rename export hit y_BurnerLoop around scan ~2016 at dt=0.010; the
test asserts ordering and reachability within a padded budget, not exact scan
numbers.
"""

from __future__ import annotations

import pytest

from tests.fixtures.tumbler import enter_production
from tests.tumbler.bench import Bench

pytestmark = pytest.mark.tumbler

# Old script waited up to 99 * 50 = 4950 scans for the burner loop; pad ~2x.
BURNER_BUDGET = 10_000

DIAG = (
    "Sts_UnitModeCurrent",
    "Sts_StateCurrent",
    "Internal__Step",
    "Rotate_CurStep",
    "Blower_CurStep",
    "Heat_CurStep",
    "HeatDelay_Tmr_Acc",
    "HeatDelay_Tmr_Done",
    "o_BurnerLoop",
    "y_BurnerLoop",
)


def test_burnerloop_prefix(tumbler_logic) -> None:
    b = Bench(tumbler_logic)

    # Physical permissives and feedback, then the first scan.
    b.force_physical()
    b.step()
    assert b.get("Sts_StateCurrent") == 9, (
        f"cold boot should settle in ABORTED(9): {b.snapshot(DIAG)}"
    )

    # Production mode.  A cold boot lands in Manual mode where all PackML
    # states are disabled; skipping this hits false-unreachable.
    enter_production(b.plc)
    b.scan = b.plc.state.scan_id
    assert b.get("Sts_UnitModeCurrent") == 1, f"Production mode not latched: {b.snapshot(DIAG)}"

    # Clear -> Reset -> Start.
    b.pulse("Cmd_State_Clear")
    b.pulse("Cmd_State_Reset")
    b.pulse("Cmd_State_Start")

    # Normal scan-time wait.  At dt=0.010:
    # - Rotate initializes around 4 s, Blower around 7 s.
    # - Execute then starts HeatDelay_Tmr; Heat_xCall follows around 10 s later.
    # - Heat reaches CurStep 3 roughly 2 s after it is called.
    execute_scan: int | None = None

    def note_execute() -> bool:
        nonlocal execute_scan
        if execute_scan is None and b.get("Sts_StateCurrent") == 6:
            execute_scan = b.scan
        return b.get("y_BurnerLoop") is True

    reached = b.step_until(note_execute, BURNER_BUDGET)
    assert reached, f"y_BurnerLoop not reached within {BURNER_BUDGET} scans: {b.snapshot(DIAG)}"

    # Stage truth at the hit: EXECUTE(6) preceded the burner loop, the SFC is
    # in the Dry step, and the internal burner output backs the physical one.
    assert execute_scan is not None, "never saw EXECUTE(6) before the burner loop"
    assert b.burner_hit_scan is not None
    assert execute_scan <= b.burner_hit_scan
    assert b.get("Sts_StateCurrent") == 6, b.snapshot(DIAG)
    assert b.get("Internal__Step") == 101, b.snapshot(DIAG)
    assert b.get("S_CurrStep_Dry") is True, b.snapshot(DIAG)
    assert b.get("Heat_CurStep") == 3, b.snapshot(DIAG)
    assert b.get("o_BurnerLoop") is True, b.snapshot(DIAG)

    # Observed vs the pre-rename export (~2016): record, don't assert.
    print(
        f"\nEXECUTE(6) at scan {execute_scan}; "
        f"y_BurnerLoop at scan {b.burner_hit_scan} (pre-rename export: ~2016)"
    )
