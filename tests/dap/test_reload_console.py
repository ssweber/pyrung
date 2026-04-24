"""Tests for the DAP reload console commands."""

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


BASIC_SCRIPT = (
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


def _setup(tmp_path: Path, script: str = BASIC_SCRIPT) -> tuple[DAPAdapter, io.BytesIO, Path]:
    out_stream = io.BytesIO()
    adapter = DAPAdapter(in_stream=io.BytesIO(), out_stream=out_stream)
    script_path = _write_script(tmp_path, "logic.py", script)
    _send_request(
        adapter, out_stream, seq=1, command="launch", arguments={"program": str(script_path)}
    )
    _send_request(adapter, out_stream, seq=2, command="configurationDone")
    _drain_messages(out_stream)
    _send_request(adapter, out_stream, seq=3, command="next")
    _drain_messages(out_stream)
    return adapter, out_stream, script_path


class TestReload:
    def test_reload_basic(self, tmp_path: Path):
        adapter, out, script_path = _setup(tmp_path)
        _repl(adapter, out, "step 5", seq=10)
        old_scan_id = adapter._runner.current_state.scan_id

        resp, stopped = _repl(adapter, out, "reload", seq=11)
        assert resp["success"] is True
        assert "Reloaded" in resp["body"]["result"]
        assert adapter._runner.current_state.scan_id == old_scan_id
        assert len(stopped) >= 1

    def test_reload_preserves_tag_values(self, tmp_path: Path):
        adapter, out, script_path = _setup(tmp_path)
        _repl(adapter, out, "force Button true", seq=10)
        _repl(adapter, out, "step 1", seq=11)
        assert adapter._runner.current_state.tags["Light"] is True

        _repl(adapter, out, "reload", seq=12)
        assert adapter._runner.current_state.tags["Light"] is True

    def test_reload_preserves_forces(self, tmp_path: Path):
        adapter, out, script_path = _setup(tmp_path)
        _repl(adapter, out, "force Button true", seq=10)
        _repl(adapter, out, "reload", seq=11)
        assert adapter._runner.forces["Button"] is True

    def test_reload_scan_continuity(self, tmp_path: Path):
        adapter, out, script_path = _setup(tmp_path)
        _repl(adapter, out, "step 5", seq=10)
        scan_before = adapter._runner.current_state.scan_id

        _repl(adapter, out, "reload", seq=11)
        _repl(adapter, out, "step 1", seq=12)
        assert adapter._runner.current_state.scan_id == scan_before + 1

    def test_reload_emits_stopped_event(self, tmp_path: Path):
        adapter, out, script_path = _setup(tmp_path)
        resp, stopped = _repl(adapter, out, "reload", seq=10)
        assert resp["success"] is True
        assert len(stopped) >= 1
        assert stopped[0]["body"]["reason"] == "entry"

    def test_reload_with_new_rung(self, tmp_path: Path):
        adapter, out, script_path = _setup(tmp_path)
        _repl(adapter, out, "force Button true", seq=10)
        _repl(adapter, out, "step 1", seq=11)
        old_scan_id = adapter._runner.current_state.scan_id

        new_script = (
            "from pyrung.core import Bool, Int, PLC, Program, Rung, out, copy\n"
            "\n"
            "button = Bool('Button')\n"
            "light = Bool('Light')\n"
            "counter = Int('Counter')\n"
            "extra = Bool('Extra')\n"
            "\n"
            "with Program(strict=False) as prog:\n"
            "    with Rung(button):\n"
            "        out(light)\n"
            "    with Rung():\n"
            "        copy(0, counter)\n"
            "    with Rung(button):\n"
            "        out(extra)\n"
            "\n"
            "runner = PLC(prog, dt=0.010)\n"
        )
        script_path.write_text(new_script, encoding="utf-8")

        resp, _ = _repl(adapter, out, "reload", seq=12)
        assert resp["success"] is True
        assert adapter._runner.current_state.scan_id == old_scan_id
        assert adapter._runner.current_state.tags["Light"] is True
        assert adapter._runner.forces["Button"] is True
        assert "Extra" in adapter._runner._known_tags_by_name


class TestReloadTagTypeChange:
    def test_type_change_drops_value(self, tmp_path: Path):
        adapter, out, script_path = _setup(tmp_path)
        _repl(adapter, out, "force Button true", seq=10)
        _repl(adapter, out, "step 1", seq=11)
        assert adapter._runner.current_state.tags["Light"] is True

        changed_script = (
            "from pyrung.core import Int, PLC, Program, Rung, copy\n"
            "\n"
            "light = Int('Light')\n"
            "counter = Int('Counter')\n"
            "\n"
            "with Program(strict=False) as prog:\n"
            "    with Rung():\n"
            "        copy(0, light)\n"
            "    with Rung():\n"
            "        copy(0, counter)\n"
            "\n"
            "runner = PLC(prog, dt=0.010)\n"
        )
        script_path.write_text(changed_script, encoding="utf-8")

        resp, _ = _repl(adapter, out, "reload", seq=12)
        assert resp["success"] is True
        assert "type changed" in resp["body"]["result"]
        assert adapter._runner.current_state.tags["Light"] == 0


class TestReloadErrors:
    def test_syntax_error(self, tmp_path: Path):
        adapter, out, script_path = _setup(tmp_path)
        old_runner = adapter._runner
        script_path.write_text("def broken(\n", encoding="utf-8")

        resp, _ = _repl(adapter, out, "reload", seq=10)
        assert resp["success"] is False
        assert adapter._runner is old_runner

    def test_no_runner(self, tmp_path: Path):
        adapter, out, script_path = _setup(tmp_path)
        old_runner = adapter._runner
        script_path.write_text("x = 42\n", encoding="utf-8")

        resp, _ = _repl(adapter, out, "reload", seq=10)
        assert resp["success"] is False
        assert adapter._runner is old_runner


class TestReloadBlockedByRecording:
    def test_reload_blocked_during_recording(self, tmp_path: Path):
        adapter, out, script_path = _setup(tmp_path)
        _repl(adapter, out, "record test_action", seq=10)
        resp, _ = _repl(adapter, out, "reload", seq=11)
        assert resp["success"] is False
        assert "recording" in resp.get("message", resp.get("body", {}).get("result", "")).lower()

    def test_reload_allowed_after_recording_stops(self, tmp_path: Path):
        adapter, out, script_path = _setup(tmp_path)
        _repl(adapter, out, "record test_action", seq=10)
        _repl(adapter, out, "patch Button true", seq=11)
        _repl(adapter, out, "step 3", seq=12)
        _repl(adapter, out, "patch Button false", seq=13)
        _repl(adapter, out, "step 3", seq=14)
        _repl(adapter, out, "patch Button true", seq=15)
        _repl(adapter, out, "step 3", seq=16)
        _repl(adapter, out, "patch Button false", seq=17)
        _repl(adapter, out, "step 3", seq=18)
        _repl(adapter, out, "record stop", seq=19)
        resp, _ = _repl(adapter, out, "reload", seq=20)
        assert resp["success"] is True
        assert "Reloaded" in resp["body"]["result"]


class TestWatchUnwatch:
    def test_watch_starts_thread(self, tmp_path: Path):
        adapter, out, script_path = _setup(tmp_path)
        resp, _ = _repl(adapter, out, "watch", seq=10)
        assert resp["success"] is True
        assert "Watching" in resp["body"]["result"]
        assert getattr(adapter, "_watch_thread", None) is not None
        assert adapter._watch_thread.is_alive()

        resp, _ = _repl(adapter, out, "unwatch", seq=11)
        assert resp["success"] is True
        assert "Stopped" in resp["body"]["result"]
        assert getattr(adapter, "_watch_thread", None) is None

    def test_watch_already_watching(self, tmp_path: Path):
        adapter, out, script_path = _setup(tmp_path)
        _repl(adapter, out, "watch", seq=10)
        resp, _ = _repl(adapter, out, "watch", seq=11)
        assert "Already watching" in resp["body"]["result"]

        _repl(adapter, out, "unwatch", seq=12)

    def test_unwatch_when_not_watching(self, tmp_path: Path):
        adapter, out, script_path = _setup(tmp_path)
        resp, _ = _repl(adapter, out, "unwatch", seq=10)
        assert "Not watching" in resp["body"]["result"]
