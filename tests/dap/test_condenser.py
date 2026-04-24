"""Tests for the DAP capture condenser."""

from __future__ import annotations

from pyrung.core import PLC, Bool, Program, Rung, out
from pyrung.dap.capture import CaptureEntry
from pyrung.dap.condenser import (
    classify_command,
    coalesce_fumbles,
    condense_capture,
    parse_provenance_line,
)


def _simple_runner() -> PLC:
    button = Bool("Button")
    light = Bool("Light")
    with Program() as prog:
        with Rung(button):
            out(light)
    return PLC(prog, dt=0.010)


def test_classifies_commands() -> None:
    assert classify_command("patch Button true").kind == "mutation"
    assert classify_command("force Button true").kind == "mutation"
    assert classify_command("unforce Button").kind == "mutation"
    assert classify_command("step 3").kind == "span"
    assert classify_command("run 500 ms").span_duration_ms == 500
    assert classify_command("upstream Button").kind == "query"
    assert classify_command("custom ok").kind == "unknown"


def test_parses_harness_provenance_with_nested_colons() -> None:
    parsed = parse_provenance_line("harness:analog:thermal: patch Temp 1.5")
    assert parsed.source == "harness:analog:thermal"
    assert parsed.command == "patch Temp 1.5"


def test_run_span_shrinks_to_last_relevant_transition() -> None:
    runner = _simple_runner()
    runner.patch({"Button": True})
    runner.run(5)

    result = condense_capture(
        "press_button",
        [
            CaptureEntry("patch Button true", scan_id=0, timestamp=0.0),
            CaptureEntry("run 5", scan_id=5, timestamp=0.05),
        ],
        runner,
        start_scan_id=0,
    )

    assert "patch Button true" in result.transcript
    assert "run 1" in result.transcript
    assert "run 5" not in result.transcript


def test_query_commands_are_dropped() -> None:
    runner = _simple_runner()
    result = condense_capture(
        "inspect",
        [
            CaptureEntry("patch Button true", scan_id=0, timestamp=0.0),
            CaptureEntry("upstream Button", scan_id=0, timestamp=0.0),
        ],
        runner,
        start_scan_id=0,
    )

    assert "patch Button true" in result.transcript
    assert "upstream Button" not in result.transcript


def test_fumble_coalescing_removes_patch_back_to_original() -> None:
    runner = _simple_runner()
    kept, coalesced = coalesce_fumbles(
        [
            CaptureEntry("patch Button true", scan_id=0, timestamp=0.0),
            CaptureEntry("patch Button false", scan_id=0, timestamp=0.0),
        ],
        runner,
        segment_start_scan=0,
    )

    assert kept == []
    assert coalesced == 2


def test_force_unforce_pair_reduces_to_final_effect() -> None:
    runner = _simple_runner()
    kept, coalesced = coalesce_fumbles(
        [
            CaptureEntry("force Button true", scan_id=0, timestamp=0.0),
            CaptureEntry("unforce Button", scan_id=0, timestamp=0.0),
        ],
        runner,
        segment_start_scan=0,
    )

    assert kept == []
    assert coalesced == 2


def test_harness_feedback_keeps_span_and_preserves_provenance() -> None:
    runner = PLC(dt=0.010)
    runner.patch({"Cmd": True})
    runner.step()
    runner.patch({"Fb": True})
    runner.step()

    result = condense_capture(
        "feedback",
        [
            CaptureEntry("patch Cmd True", scan_id=0, timestamp=0.0),
            CaptureEntry(
                "patch Fb True",
                scan_id=1,
                timestamp=0.01,
                provenance="harness:nominal",
            ),
            CaptureEntry("run 2", scan_id=2, timestamp=0.02),
        ],
        runner,
        start_scan_id=0,
    )

    assert "harness:nominal: patch Fb True" in result.transcript
    assert "run 1" in result.transcript


def test_unrelated_free_running_change_does_not_keep_run_alive() -> None:
    runner = _simple_runner()
    runner.patch({"Noise": True})
    runner.run(1)

    result = condense_capture(
        "ignore_noise",
        [
            CaptureEntry("patch Button false", scan_id=0, timestamp=0.0),
            CaptureEntry("run 1", scan_id=1, timestamp=0.01),
        ],
        runner,
        start_scan_id=0,
    )

    assert "run 1" not in result.transcript
    assert "patch Button false" not in result.transcript
