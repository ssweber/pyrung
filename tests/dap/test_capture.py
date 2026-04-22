"""Tests for the session capture buffer and record verb."""

from __future__ import annotations

import io
from pathlib import Path
from typing import Any

from pyrung.dap.adapter import DAPAdapter
from pyrung.dap.capture import CaptureBuffer, CaptureEntry
from pyrung.dap.protocol import read_message

# ---------------------------------------------------------------------------
# Helpers (same pattern as test_console.py)
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
# CaptureBuffer unit tests
# ---------------------------------------------------------------------------


class TestCaptureBuffer:
    def test_not_recording_initially(self):
        buf = CaptureBuffer()
        assert buf.recording is False

    def test_start_sets_recording(self):
        buf = CaptureBuffer()
        buf.start("test_action", scan_id=5, timestamp=1.0)
        assert buf.recording is True
        assert buf.action == "test_action"
        assert buf.start_scan_id == 5
        assert buf.start_timestamp == 1.0

    def test_append_ignored_when_not_recording(self):
        buf = CaptureBuffer()
        buf.append("step 1", scan_id=1, timestamp=0.0)
        assert buf.entries == []

    def test_append_when_recording(self):
        buf = CaptureBuffer()
        buf.start("act", scan_id=0, timestamp=0.0)
        buf.append("patch X 1", scan_id=1, timestamp=0.01)
        buf.append("step 1", scan_id=1, timestamp=0.01)
        assert len(buf.entries) == 2
        assert buf.entries[0] == CaptureEntry("patch X 1", 1, 0.01)

    def test_stop_returns_transcript(self):
        buf = CaptureBuffer()
        buf.start("start_machine", scan_id=0, timestamp=0.0)
        buf.append("patch State 1", scan_id=1, timestamp=0.01)
        buf.append("run 500 ms", scan_id=1, timestamp=0.01)
        buf.append("patch State 2", scan_id=50, timestamp=0.51)
        buf.append("step 1", scan_id=50, timestamp=0.51)
        transcript, entries = buf.stop()

        assert "# action: start_machine" in transcript
        assert "patch State 1" in transcript
        assert "run 500 ms" in transcript
        assert "patch State 2" in transcript
        assert "step 1" in transcript
        assert len(entries) == 4
        assert buf.recording is False

    def test_stop_clears_state(self):
        buf = CaptureBuffer()
        buf.start("act", scan_id=0, timestamp=0.0)
        buf.append("step 1", scan_id=1, timestamp=0.01)
        buf.stop()
        assert buf.action is None
        assert buf.entries == []

    def test_reset(self):
        buf = CaptureBuffer()
        buf.start("act", scan_id=5, timestamp=1.0)
        buf.append("step 1", scan_id=6, timestamp=1.01)
        buf.reset()
        assert buf.recording is False
        assert buf.entries == []
        assert buf.start_scan_id is None


# ---------------------------------------------------------------------------
# Record verb via DAP
# ---------------------------------------------------------------------------


class TestRecordVerb:
    def test_record_start(self, tmp_path: Path):
        adapter, out = _setup(tmp_path)
        resp, _ = _repl(adapter, out, "record start_machine")
        assert resp["success"] is True
        assert "Recording 'start_machine'" in resp["body"]["result"]
        assert adapter._capture.recording is True
        assert adapter._capture.action == "start_machine"

    def test_record_stop(self, tmp_path: Path):
        adapter, out = _setup(tmp_path)
        _repl(adapter, out, "record start_machine", seq=10)
        _repl(adapter, out, "patch Button true", seq=11)
        _repl(adapter, out, "step 1", seq=12)
        resp, _ = _repl(adapter, out, "record stop", seq=13)
        assert resp["success"] is True
        result = resp["body"]["result"]
        assert "Recording stopped." in result
        assert "# action: start_machine" in result
        assert "patch Button true" in result
        assert "step 1" in result
        assert adapter._capture.recording is False

    def test_record_stop_without_active(self, tmp_path: Path):
        adapter, out = _setup(tmp_path)
        resp, _ = _repl(adapter, out, "record stop")
        assert resp["success"] is False
        assert "No active recording" in resp["message"]

    def test_record_while_already_recording(self, tmp_path: Path):
        adapter, out = _setup(tmp_path)
        _repl(adapter, out, "record first_action", seq=10)
        resp, _ = _repl(adapter, out, "record second_action", seq=11)
        assert resp["success"] is False
        assert "Already recording" in resp["message"]

    def test_record_missing_action_name(self, tmp_path: Path):
        adapter, out = _setup(tmp_path)
        resp, _ = _repl(adapter, out, "record")
        assert resp["success"] is False
        assert "Usage:" in resp["message"]

    def test_record_warns_on_active_forces(self, tmp_path: Path):
        adapter, out = _setup(tmp_path)
        _repl(adapter, out, "force Button true", seq=10)
        resp, _ = _repl(adapter, out, "record with_forces", seq=11)
        assert resp["success"] is True
        assert "Warning: active forces" in resp["body"]["result"]
        assert "Button" in resp["body"]["result"]


# ---------------------------------------------------------------------------
# Dispatch hook — passive capture
# ---------------------------------------------------------------------------


class TestCaptureHook:
    def test_commands_captured_during_recording(self, tmp_path: Path):
        adapter, out = _setup(tmp_path)
        _repl(adapter, out, "record my_action", seq=10)
        _repl(adapter, out, "patch Button true", seq=11)
        _repl(adapter, out, "step 1", seq=12)
        assert len(adapter._capture.entries) == 2
        assert adapter._capture.entries[0].command == "patch Button true"
        assert adapter._capture.entries[1].command == "step 1"

    def test_commands_not_captured_when_not_recording(self, tmp_path: Path):
        adapter, out = _setup(tmp_path)
        _repl(adapter, out, "patch Button true", seq=10)
        _repl(adapter, out, "step 1", seq=11)
        assert adapter._capture.entries == []

    def test_record_and_help_excluded_from_capture(self, tmp_path: Path):
        adapter, out = _setup(tmp_path)
        _repl(adapter, out, "record my_action", seq=10)
        _repl(adapter, out, "help", seq=11)
        _repl(adapter, out, "patch Button true", seq=12)
        assert len(adapter._capture.entries) == 1
        assert adapter._capture.entries[0].command == "patch Button true"

    def test_entries_have_scan_id(self, tmp_path: Path):
        adapter, out = _setup(tmp_path)
        _repl(adapter, out, "record my_action", seq=10)
        _repl(adapter, out, "patch Button true", seq=11)
        entry = adapter._capture.entries[0]
        assert entry.scan_id is not None

    def test_entries_have_timestamp(self, tmp_path: Path):
        adapter, out = _setup(tmp_path)
        _repl(adapter, out, "record my_action", seq=10)
        _repl(adapter, out, "step 3", seq=11)
        entry = adapter._capture.entries[0]
        assert entry.timestamp >= 0.0

    def test_query_verbs_captured(self, tmp_path: Path):
        adapter, out = _setup(tmp_path)
        _repl(adapter, out, "record my_action", seq=10)
        _repl(adapter, out, "step 1", seq=11)
        _repl(adapter, out, "upstream Button", seq=12)
        assert len(adapter._capture.entries) == 2
        assert adapter._capture.entries[1].command == "upstream Button"


# ---------------------------------------------------------------------------
# Transcript format
# ---------------------------------------------------------------------------


class TestTranscriptFormat:
    def test_transcript_is_replayable(self, tmp_path: Path):
        """Transcript lines are valid console commands (minus the action comment)."""
        adapter, out = _setup(tmp_path)
        _repl(adapter, out, "record replay_test", seq=10)
        _repl(adapter, out, "patch Button true", seq=11)
        _repl(adapter, out, "step 2", seq=12)
        resp, _ = _repl(adapter, out, "record stop", seq=13)
        transcript = resp["body"]["result"].split("\n", 1)[1]  # skip "Recording stopped."

        lines = [line for line in transcript.strip().splitlines() if not line.startswith("#")]
        assert lines == ["patch Button true", "step 2"]

    def test_transcript_starts_with_action_comment(self, tmp_path: Path):
        adapter, out = _setup(tmp_path)
        _repl(adapter, out, "record hello_world", seq=10)
        _repl(adapter, out, "step 1", seq=11)
        resp, _ = _repl(adapter, out, "record stop", seq=12)
        transcript = resp["body"]["result"].split("\n", 1)[1]
        assert transcript.startswith("# action: hello_world")

    def test_multiple_recordings_independent(self, tmp_path: Path):
        adapter, out = _setup(tmp_path)
        _repl(adapter, out, "record first", seq=10)
        _repl(adapter, out, "patch Button true", seq=11)
        resp1, _ = _repl(adapter, out, "record stop", seq=12)

        _repl(adapter, out, "record second", seq=13)
        _repl(adapter, out, "step 1", seq=14)
        resp2, _ = _repl(adapter, out, "record stop", seq=15)

        t1 = resp1["body"]["result"]
        t2 = resp2["body"]["result"]
        assert "# action: first" in t1
        assert "patch Button true" in t1
        assert "step 1" not in t1

        assert "# action: second" in t2
        assert "step 1" in t2
        assert "patch Button true" not in t2


# ---------------------------------------------------------------------------
# Help listing includes record
# ---------------------------------------------------------------------------


class TestRecordInHelp:
    def test_help_lists_record(self, tmp_path: Path):
        adapter, out = _setup(tmp_path)
        resp, _ = _repl(adapter, out, "help")
        assert "record" in resp["body"]["result"]
