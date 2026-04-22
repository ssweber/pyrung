"""Tests for the DAP console command dispatcher."""

from __future__ import annotations

import io
from pathlib import Path
from typing import Any

from pyrung.dap.adapter import DAPAdapter
from pyrung.dap.protocol import read_message


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


def _write_script(tmp_path: Path, name: str, content: str) -> Path:
    script_path = tmp_path / name
    script_path.write_text(content, encoding="utf-8")
    return script_path


def _single_response(messages: list[dict[str, Any]]) -> dict[str, Any]:
    responses = [msg for msg in messages if msg.get("type") == "response"]
    assert len(responses) == 1
    return responses[0]


def _stopped_events(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [msg for msg in messages if msg.get("type") == "event" and msg.get("event") == "stopped"]


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
    script = _write_script(tmp_path, "logic.py", _runner_script())
    _send_request(adapter, out_stream, seq=1, command="launch", arguments={"program": str(script)})
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
    stopped = _stopped_events(messages)
    return response, stopped


# ---------------------------------------------------------------------------
# Existing verbs (regression)
# ---------------------------------------------------------------------------


class TestForceVerbs:
    def test_force(self, tmp_path: Path):
        adapter, out = _setup(tmp_path)
        resp, _ = _repl(adapter, out, "force Button true")
        assert resp["success"] is True
        assert adapter._runner.forces["Button"] is True

    def test_unforce(self, tmp_path: Path):
        adapter, out = _setup(tmp_path)
        _repl(adapter, out, "force Button true", seq=10)
        resp, _ = _repl(adapter, out, "unforce Button", seq=11)
        assert resp["success"] is True
        assert "Button" not in adapter._runner.forces

    def test_clear_forces(self, tmp_path: Path):
        adapter, out = _setup(tmp_path)
        _repl(adapter, out, "force Button true", seq=10)
        resp, _ = _repl(adapter, out, "clear_forces", seq=11)
        assert resp["success"] is True
        assert dict(adapter._runner.forces) == {}


# ---------------------------------------------------------------------------
# Patch
# ---------------------------------------------------------------------------


class TestPatch:
    def test_patch_sets_value(self, tmp_path: Path):
        adapter, out = _setup(tmp_path)
        resp, _ = _repl(adapter, out, "patch Button true")
        assert resp["success"] is True
        assert "Patched" in resp["body"]["result"]

    def test_patch_missing_args(self, tmp_path: Path):
        adapter, out = _setup(tmp_path)
        resp, _ = _repl(adapter, out, "patch Button")
        assert resp["success"] is False


# ---------------------------------------------------------------------------
# Step
# ---------------------------------------------------------------------------


class TestStep:
    def test_step_one(self, tmp_path: Path):
        adapter, out = _setup(tmp_path)
        scan_before = adapter._runner.current_state.scan_id
        resp, stopped = _repl(adapter, out, "step")
        assert resp["success"] is True
        assert "1 scan(s)" in resp["body"]["result"]
        assert adapter._runner.current_state.scan_id == scan_before + 1
        assert len(stopped) >= 1

    def test_step_n(self, tmp_path: Path):
        adapter, out = _setup(tmp_path)
        scan_before = adapter._runner.current_state.scan_id
        resp, stopped = _repl(adapter, out, "step 5")
        assert resp["success"] is True
        assert "5 scan(s)" in resp["body"]["result"]
        assert adapter._runner.current_state.scan_id == scan_before + 5
        assert len(stopped) >= 1

    def test_step_bad_count(self, tmp_path: Path):
        adapter, out = _setup(tmp_path)
        resp, _ = _repl(adapter, out, "step abc")
        assert resp["success"] is False

    def test_step_zero(self, tmp_path: Path):
        adapter, out = _setup(tmp_path)
        resp, _ = _repl(adapter, out, "step 0")
        assert resp["success"] is False

    def test_step_emits_stopped_event(self, tmp_path: Path):
        adapter, out = _setup(tmp_path)
        _, stopped = _repl(adapter, out, "step")
        assert len(stopped) >= 1
        assert stopped[0]["body"]["reason"] == "step"


# ---------------------------------------------------------------------------
# Run
# ---------------------------------------------------------------------------


class TestRun:
    def test_run_cycles(self, tmp_path: Path):
        adapter, out = _setup(tmp_path)
        scan_before = adapter._runner.current_state.scan_id
        resp, stopped = _repl(adapter, out, "run 10")
        assert resp["success"] is True
        assert "10 cycle(s)" in resp["body"]["result"]
        assert adapter._runner.current_state.scan_id == scan_before + 10
        assert len(stopped) >= 1

    def test_run_duration(self, tmp_path: Path):
        adapter, out = _setup(tmp_path)
        ts_before = adapter._runner.current_state.timestamp
        resp, stopped = _repl(adapter, out, "run 100ms")
        assert resp["success"] is True
        assert "scan(s)" in resp["body"]["result"]
        ts_after = adapter._runner.current_state.timestamp
        assert ts_after - ts_before >= 0.099
        assert len(stopped) >= 1

    def test_run_duration_seconds(self, tmp_path: Path):
        adapter, out = _setup(tmp_path)
        ts_before = adapter._runner.current_state.timestamp
        resp, _ = _repl(adapter, out, "run 1s")
        assert resp["success"] is True
        ts_after = adapter._runner.current_state.timestamp
        assert ts_after - ts_before >= 0.999

    def test_run_missing_spec(self, tmp_path: Path):
        adapter, out = _setup(tmp_path)
        resp, _ = _repl(adapter, out, "run")
        assert resp["success"] is False

    def test_run_bad_spec(self, tmp_path: Path):
        adapter, out = _setup(tmp_path)
        resp, _ = _repl(adapter, out, "run foobar")
        assert resp["success"] is False


# ---------------------------------------------------------------------------
# Cause / Effect / Recovers
# ---------------------------------------------------------------------------


class TestCausalVerbs:
    def test_cause_with_transition(self, tmp_path: Path):
        adapter, out = _setup(tmp_path)
        _repl(adapter, out, "patch Button true", seq=10)
        _repl(adapter, out, "step", seq=11)
        resp, _ = _repl(adapter, out, "cause Light", seq=12)
        assert resp["success"] is True
        result = resp["body"]["result"]
        assert "Light" in result

    def test_cause_no_chain(self, tmp_path: Path):
        adapter, out = _setup(tmp_path)
        resp, _ = _repl(adapter, out, "cause Light")
        assert resp["success"] is True
        assert "No causal chain" in resp["body"]["result"]

    def test_cause_missing_tag(self, tmp_path: Path):
        adapter, out = _setup(tmp_path)
        resp, _ = _repl(adapter, out, "cause")
        assert resp["success"] is False

    def test_effect(self, tmp_path: Path):
        adapter, out = _setup(tmp_path)
        _repl(adapter, out, "patch Button true", seq=10)
        _repl(adapter, out, "step", seq=11)
        resp, _ = _repl(adapter, out, "effect Button", seq=12)
        assert resp["success"] is True

    def test_recovers(self, tmp_path: Path):
        adapter, out = _setup(tmp_path)
        resp, _ = _repl(adapter, out, "recovers Light")
        assert resp["success"] is True
        assert "recovers:" in resp["body"]["result"]


# ---------------------------------------------------------------------------
# DataView / Upstream / Downstream
# ---------------------------------------------------------------------------


class TestDataViewVerbs:
    def test_dataview_contains(self, tmp_path: Path):
        adapter, out = _setup(tmp_path)
        resp, _ = _repl(adapter, out, "dataview Button")
        assert resp["success"] is True
        assert "Button" in resp["body"]["result"]

    def test_dataview_role_prefix(self, tmp_path: Path):
        adapter, out = _setup(tmp_path)
        resp, _ = _repl(adapter, out, "dataview t:")
        assert resp["success"] is True
        assert "tag(s)" in resp["body"]["result"]

    def test_dataview_no_match(self, tmp_path: Path):
        adapter, out = _setup(tmp_path)
        resp, _ = _repl(adapter, out, "dataview ZZZnonexistent")
        assert resp["success"] is True
        assert "No matching" in resp["body"]["result"]

    def test_dataview_missing_query(self, tmp_path: Path):
        adapter, out = _setup(tmp_path)
        resp, _ = _repl(adapter, out, "dataview")
        assert resp["success"] is False

    def test_upstream(self, tmp_path: Path):
        adapter, out = _setup(tmp_path)
        resp, _ = _repl(adapter, out, "upstream Light")
        assert resp["success"] is True
        assert "Button" in resp["body"]["result"]

    def test_downstream(self, tmp_path: Path):
        adapter, out = _setup(tmp_path)
        resp, _ = _repl(adapter, out, "downstream Button")
        assert resp["success"] is True
        assert "Light" in resp["body"]["result"]

    def test_upstream_missing_tag(self, tmp_path: Path):
        adapter, out = _setup(tmp_path)
        resp, _ = _repl(adapter, out, "upstream")
        assert resp["success"] is False

    def test_downstream_missing_tag(self, tmp_path: Path):
        adapter, out = _setup(tmp_path)
        resp, _ = _repl(adapter, out, "downstream")
        assert resp["success"] is False


# ---------------------------------------------------------------------------
# Monitor / Unmonitor
# ---------------------------------------------------------------------------


class TestMonitorVerbs:
    def test_monitor_adds(self, tmp_path: Path):
        adapter, out = _setup(tmp_path)
        resp, _ = _repl(adapter, out, "monitor Button")
        assert resp["success"] is True
        assert "Monitor added" in resp["body"]["result"]
        assert len(adapter._monitor_meta) == 1

    def test_unmonitor_removes(self, tmp_path: Path):
        adapter, out = _setup(tmp_path)
        _repl(adapter, out, "monitor Button", seq=10)
        resp, _ = _repl(adapter, out, "unmonitor Button", seq=11)
        assert resp["success"] is True
        assert "Monitor removed" in resp["body"]["result"]
        assert len(adapter._monitor_meta) == 0

    def test_unmonitor_unknown_tag(self, tmp_path: Path):
        adapter, out = _setup(tmp_path)
        resp, _ = _repl(adapter, out, "unmonitor NonExistent")
        assert resp["success"] is False

    def test_monitor_missing_tag(self, tmp_path: Path):
        adapter, out = _setup(tmp_path)
        resp, _ = _repl(adapter, out, "monitor")
        assert resp["success"] is False


# ---------------------------------------------------------------------------
# Simplified
# ---------------------------------------------------------------------------


class TestSimplifiedVerb:
    def test_simplified_single_tag(self, tmp_path: Path):
        adapter, out = _setup(tmp_path)
        resp, _ = _repl(adapter, out, "simplified Light")
        assert resp["success"] is True
        result = resp["body"]["result"]
        assert "Light = Button" in result
        assert "writer(s)" in result

    def test_simplified_all(self, tmp_path: Path):
        adapter, out = _setup(tmp_path)
        resp, _ = _repl(adapter, out, "simplified")
        assert resp["success"] is True
        result = resp["body"]["result"]
        assert "terminal(s)" in result
        assert "Light" in result

    def test_simplified_non_terminal(self, tmp_path: Path):
        adapter, out = _setup(tmp_path)
        resp, _ = _repl(adapter, out, "simplified Button")
        assert resp["success"] is False
        assert "not a terminal" in resp["message"]

    def test_simplified_unknown_tag(self, tmp_path: Path):
        adapter, out = _setup(tmp_path)
        resp, _ = _repl(adapter, out, "simplified ZZZnonexistent")
        assert resp["success"] is False
        assert "Unknown tag" in resp["message"]


# ---------------------------------------------------------------------------
# Help and error handling
# ---------------------------------------------------------------------------


class TestHelpAndErrors:
    def test_help_lists_all_verbs(self, tmp_path: Path):
        adapter, out = _setup(tmp_path)
        resp, _ = _repl(adapter, out, "help")
        assert resp["success"] is True
        result = resp["body"]["result"]
        for verb in [
            "force",
            "unforce",
            "patch",
            "step",
            "run",
            "cause",
            "effect",
            "monitor",
            "simplified",
            "help",
        ]:
            assert verb in result

    def test_unknown_command(self, tmp_path: Path):
        adapter, out = _setup(tmp_path)
        resp, _ = _repl(adapter, out, "foobar")
        assert resp["success"] is False
        assert "Unknown command" in resp["message"]
        assert "Available:" in resp["message"]

    def test_unknown_command_still_mentions_watch(self, tmp_path: Path):
        adapter, out = _setup(tmp_path)
        resp, _ = _repl(adapter, out, "Button == true")
        assert resp["success"] is False
        assert "Watch" in resp["message"]
