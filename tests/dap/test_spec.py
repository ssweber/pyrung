"""Tests for spec formula parsing and test generation."""

from __future__ import annotations

import io
from pathlib import Path
from typing import Any

from pyrung.dap.adapter import DAPAdapter
from pyrung.dap.protocol import read_message
from pyrung.dap.spec import (
    generate_test_file,
    parse_formula,
)

# ---------------------------------------------------------------------------
# Helpers (same pattern as test_capture.py)
# ---------------------------------------------------------------------------


def _drain_messages(stream: io.BytesIO) -> list[dict[str, Any]]:
    data = stream.getvalue()
    reader = io.BytesIO(data)
    messages: list[dict[str, Any]] = []
    while True:
        message = read_message(reader)
        if message is None:
            break
        messages.append(message)
    stream.seek(0)
    stream.truncate(0)
    return messages


def _send_request(
    adapter: DAPAdapter,
    out_stream: io.BytesIO,
    *,
    seq: int,
    command: str,
    arguments: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    adapter.handle_request(
        {"seq": seq, "type": "request", "command": command, "arguments": arguments or {}}
    )
    return _drain_messages(out_stream)


def _single_response(messages: list[dict[str, Any]]) -> dict[str, Any]:
    responses = [msg for msg in messages if msg.get("type") == "response"]
    assert len(responses) == 1
    return responses[0]


def _runner_script() -> str:
    return (
        "from pyrung.core import Bool, Int, PLC, Program, Rung, out, copy\n"
        "\n"
        "button = Bool('Button')\n"
        "light = Bool('Light')\n"
        "counter = Int('Counter')\n"
        "\n"
        "with Program(strict=False) as prog:\n"
        "    with Rung(button):\n"
        "        out(light)\n"
        "    with Rung():\n"
        "        copy(0, counter)\n"
        "\n"
        "runner = PLC(prog, dt=0.010)\n"
    )


def _setup(tmp_path: Path) -> tuple[DAPAdapter, io.BytesIO]:
    out_stream = io.BytesIO()
    adapter = DAPAdapter(in_stream=io.BytesIO(), out_stream=out_stream)
    script_path = tmp_path / "logic.py"
    script_path.write_text(_runner_script(), encoding="utf-8")
    _send_request(
        adapter, out_stream, seq=1, command="launch", arguments={"program": str(script_path)}
    )
    _send_request(adapter, out_stream, seq=2, command="configurationDone")
    _drain_messages(out_stream)
    _send_request(adapter, out_stream, seq=3, command="next")
    _drain_messages(out_stream)
    return adapter, out_stream


def _repl(
    adapter: DAPAdapter, out_stream: io.BytesIO, expression: str, *, seq: int = 10
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    messages = _send_request(
        adapter,
        out_stream,
        seq=seq,
        command="evaluate",
        arguments={"expression": expression, "context": "repl"},
    )
    response = _single_response(messages)
    stopped = [
        msg for msg in messages if msg.get("type") == "event" and msg.get("event") == "stopped"
    ]
    return response, stopped


# ---------------------------------------------------------------------------
# Formula parser unit tests
# ---------------------------------------------------------------------------


class TestParseFormula:
    def test_edge_correlation(self):
        entry = parse_formula("Button↑ -> Light↑ within 0 scans [dt=0.01]")
        assert entry.kind == "edge_correlation"
        assert entry.antecedent_tag == "Button"
        assert entry.consequent_tag == "Light"
        assert entry.antecedent_direction == "up"
        assert entry.consequent_direction == "up"
        assert entry.delay_scans == 0
        assert entry.dt_seconds == 0.01

    def test_edge_down(self):
        entry = parse_formula("Switch↓ -> Valve↓ within 3 scans [dt=0.005]")
        assert entry.kind == "edge_correlation"
        assert entry.antecedent_direction == "down"
        assert entry.consequent_direction == "down"
        assert entry.delay_scans == 3
        assert entry.dt_seconds == 0.005

    def test_steady_implication(self):
        entry = parse_formula("Running => ~Fault [dt=0.01]")
        assert entry.kind == "steady_implication"
        assert entry.antecedent_tag == "Running"
        assert entry.consequent_tag == "Fault"
        assert entry.negated is True
        assert entry.dt_seconds == 0.01

    def test_steady_implication_positive(self):
        entry = parse_formula("A => B [dt=0.02]")
        assert entry.kind == "steady_implication"
        assert entry.negated is False

    def test_value_temporal(self):
        entry = parse_formula("State=2 => MotorOut=true within 0 scans [dt=0.01]")
        assert entry.kind == "value_temporal"
        assert entry.antecedent_tag == "State"
        assert entry.antecedent_value == 2
        assert entry.consequent_tag == "MotorOut"
        assert entry.consequent_value is True
        assert entry.delay_scans == 0

    def test_unrecognised_raises(self):
        import pytest

        with pytest.raises(ValueError, match="Unrecognised"):
            parse_formula("not a valid formula")


# ---------------------------------------------------------------------------
# Test generation
# ---------------------------------------------------------------------------

_PROGRAM_SOURCE = (
    "from pyrung.core import Bool, PLC, Program, Rung, out\n"
    "\n"
    "button = Bool('Button')\n"
    "light = Bool('Light')\n"
    "\n"
    "with Program(strict=False) as prog:\n"
    "    with Rung(button):\n"
    "        out(light)"
)


class TestGenerateTest:
    def test_edge_up_within_0(self):
        spec = parse_formula("Button↑ -> Light↑ within 0 scans [dt=0.01]")
        output = generate_test_file([spec], _PROGRAM_SOURCE)
        assert "def test_button_up_light_up():" in output
        assert 'plc.patch({"Button": True})' in output
        assert "plc.step()" in output
        assert 'assert plc.current_state.tags["Light"]' in output

    def test_edge_up_within_3(self):
        spec = parse_formula("Button↑ -> Light↑ within 3 scans [dt=0.01]")
        output = generate_test_file([spec], _PROGRAM_SOURCE)
        assert "plc.run(cycles=4)" in output

    def test_edge_down(self):
        spec = parse_formula("Switch↓ -> Valve↓ within 0 scans [dt=0.01]")
        output = generate_test_file([spec], _PROGRAM_SOURCE)
        assert "def test_switch_down_valve_down():" in output
        assert 'plc.patch({"Switch": True})' in output
        assert 'plc.patch({"Switch": False})' in output
        assert 'assert not plc.current_state.tags["Valve"]' in output

    def test_implication_structural_tier1(self):
        from pyrung.core import Bool, Program, Rung, out

        A = Bool("A")
        B = Bool("B")
        with Program(strict=False) as prog:
            with Rung(A):
                out(B)

        spec = parse_formula("B => A [dt=0.01]")
        output = generate_test_file([spec], _PROGRAM_SOURCE, program=prog)
        assert "def test_b_implies_a():" in output
        assert "expr_requires" in output
        assert "plc.force" not in output

    def test_implication_structural_tier2(self):
        from pyrung.core import Bool, Program, Rung, latch, reset

        Guard = Bool("Guard")
        Latched = Bool("Latched")
        Trigger = Bool("Trigger")
        with Program(strict=False) as prog:
            with Rung(Trigger):
                latch(Latched)
            with Rung(~Guard):
                reset(Latched)

        spec = parse_formula("Latched => Guard [dt=0.01]")
        output = generate_test_file([spec], _PROGRAM_SOURCE, program=prog)
        assert "def test_latched_implies_guard():" in output
        assert "reset_dominance" in output
        assert "plc.force" not in output

    def test_implication_unverifiable_skipped(self):
        spec = parse_formula("Running => ~Fault [dt=0.01]")
        output = generate_test_file([spec], _PROGRAM_SOURCE)
        assert "def test_running_implies_not_fault():" in output
        assert "pytest.mark.skip" in output
        assert "plc.force" not in output

    def test_implication_no_program_skipped(self):
        spec = parse_formula("A => B [dt=0.02]")
        output = generate_test_file([spec], _PROGRAM_SOURCE)
        assert "def test_a_implies_b():" in output
        assert "pytest.mark.skip" in output
        assert "    pass" in output

    def test_value_temporal(self):
        spec = parse_formula("State=2 => MotorOut=true within 0 scans [dt=0.01]")
        output = generate_test_file([spec], _PROGRAM_SOURCE)
        assert "def test_state_2_motorout_true():" in output
        assert 'plc.patch({"State": 2})' in output
        assert 'assert plc.current_state.tags["MotorOut"] == True' in output

    def test_program_source_embedded(self):
        spec = parse_formula("Button↑ -> Light↑ within 0 scans [dt=0.01]")
        output = generate_test_file([spec], _PROGRAM_SOURCE)
        assert "with Program(strict=False) as prog:" in output
        assert "button = Bool('Button')" in output

    def test_formula_as_comment(self):
        spec = parse_formula("Button↑ -> Light↑ within 0 scans [dt=0.01]")
        output = generate_test_file([spec], _PROGRAM_SOURCE)
        assert "# Button↑ -> Light↑ within 0 scans [dt=0.01]" in output

    def test_function_name_deduplication(self):
        spec = parse_formula("Button↑ -> Light↑ within 0 scans [dt=0.01]")
        output = generate_test_file([spec, spec], _PROGRAM_SOURCE)
        assert "def test_button_up_light_up():" in output
        assert "def test_button_up_light_up_2():" in output


# ---------------------------------------------------------------------------
# Console verb: spec
# ---------------------------------------------------------------------------


class TestSpecConsole:
    def test_spec_no_accepted(self, tmp_path: Path):
        adapter, out = _setup(tmp_path)
        resp, _ = _repl(adapter, out, "spec")
        assert resp["success"] is True
        assert "No accepted specs" in resp["body"]["result"]

    def test_spec_excluded_from_capture(self, tmp_path: Path):
        adapter, out = _setup(tmp_path)
        _repl(adapter, out, "record my_action", seq=10)
        _repl(adapter, out, "spec", seq=11)
        commands = [e.command for e in adapter._capture.entries]
        assert not any("spec" in c for c in commands)

    def test_spec_test_no_accepted(self, tmp_path: Path):
        adapter, out = _setup(tmp_path)
        resp, _ = _repl(adapter, out, f"spec test {tmp_path / 'out.py'}")
        assert resp["success"] is False
        assert "No accepted specs" in resp["message"]

    def test_spec_test_missing_filepath(self, tmp_path: Path):
        adapter, out = _setup(tmp_path)
        resp, _ = _repl(adapter, out, "spec test")
        assert resp["success"] is False
        assert "Usage:" in resp["message"]


# ---------------------------------------------------------------------------
# Replay no longer checks specs
# ---------------------------------------------------------------------------


class TestReplayNoSpecs:
    def test_replay_ignores_spec_lines(self, tmp_path: Path):
        adapter, out = _setup(tmp_path)
        transcript = tmp_path / "session.txt"
        transcript.write_text(
            ("# action: test\n# spec: Button↑ -> Light↑ within 0 scans [dt=0.01]\nstep 2\n"),
            encoding="utf-8",
        )
        resp, _ = _repl(adapter, out, f"replay {transcript}")
        assert resp["success"] is True
        assert "Spec check:" not in resp["body"]["result"]
