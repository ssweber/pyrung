"""Check that the rung views of a captured scan keep reading the state the scan started from."""

from __future__ import annotations

from pyrung import PLC, Int, Program, Rung, copy
from pyrung.core.executor import ConditionViewCapture


def test_a_captured_view_reads_the_start_value_of_a_tag_a_later_rung_writes() -> None:
    ptr, seen = Int("Ptr", external=True), Int("Seen")
    with Program(strict=False) as prog:
        with Rung():
            copy(ptr, seen)
        with Rung():
            copy(ptr + 4, ptr)
    plc = PLC(prog, dt=0.01)
    plc.patch({"Ptr": 13})
    plc.step()
    assert plc.current_state.tags["Ptr"] == 17

    captured: list[ConditionViewCapture] = []
    plc._run_single_scan(
        consume_pause_request=False,
        capture_execution=True,
        capture_sink=lambda _scan_id, capture: captured.append(capture),
    )
    assert plc.current_state.tags["Ptr"] == 21

    (first, _second) = captured[0].runs
    assert first.view.get_tag("Ptr") == 17
    # The scan after a captured one reads the committed state.
    plc.step()
    assert plc.current_state.tags["Seen"] == 21
    assert plc.current_state.tags["Ptr"] == 25
