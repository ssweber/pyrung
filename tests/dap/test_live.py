"""Tests for the pyrung-live server and client."""

from __future__ import annotations

import io
from pathlib import Path
from typing import Any

import pytest

from pyrung.dap.adapter import DAPAdapter
from pyrung.dap.live import list_sessions, send_command
from pyrung.dap.protocol import read_message

# ---------------------------------------------------------------------------
# Helpers
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


def _setup_with_session(tmp_path: Path, session_name: str) -> tuple[DAPAdapter, io.BytesIO]:
    out_stream = io.BytesIO()
    adapter = DAPAdapter(in_stream=io.BytesIO(), out_stream=out_stream)
    script_path = tmp_path / "logic.py"
    script_path.write_text(_runner_script(), encoding="utf-8")
    _send_request(
        adapter,
        out_stream,
        seq=1,
        command="launch",
        arguments={"program": str(script_path), "session": session_name},
    )
    _send_request(adapter, out_stream, seq=2, command="configurationDone")
    _drain_messages(out_stream)
    _send_request(adapter, out_stream, seq=3, command="next")
    _drain_messages(out_stream)
    return adapter, out_stream


@pytest.fixture()
def live_session(tmp_path: Path):
    """Yield (adapter, out_stream, session_name) with a running LiveServer."""
    session_name = f"test_{id(tmp_path)}"
    adapter, out_stream = _setup_with_session(tmp_path, session_name)
    yield adapter, out_stream, session_name
    if adapter._live_server is not None:
        adapter._live_server.stop()


# ---------------------------------------------------------------------------
# Server lifecycle
# ---------------------------------------------------------------------------


class TestServerLifecycle:
    def test_server_starts_on_launch(self, live_session: Any):
        adapter, _out, session_name = live_session
        assert adapter._live_server is not None
        assert adapter._live_server.session_name == session_name

    def test_session_name_stored(self, live_session: Any):
        adapter, _out, session_name = live_session
        assert adapter._session.session_name == session_name

    def test_session_name_defaults_to_stem(self, tmp_path: Path):
        out_stream = io.BytesIO()
        adapter = DAPAdapter(in_stream=io.BytesIO(), out_stream=out_stream)
        script_path = tmp_path / "my_program.py"
        script_path.write_text(_runner_script(), encoding="utf-8")
        _send_request(
            adapter,
            out_stream,
            seq=1,
            command="launch",
            arguments={"program": str(script_path)},
        )
        assert adapter._session.session_name == "my_program"
        if adapter._live_server is not None:
            adapter._live_server.stop()

    def test_server_stops_on_disconnect(self, tmp_path: Path):
        session_name = f"test_disconnect_{id(tmp_path)}"
        adapter, out_stream = _setup_with_session(tmp_path, session_name)
        assert adapter._live_server is not None
        _send_request(adapter, out_stream, seq=10, command="disconnect")
        assert adapter._live_server is None


# ---------------------------------------------------------------------------
# Client ↔ Server communication
# ---------------------------------------------------------------------------


class TestClientServer:
    def test_send_command_success(self, live_session: Any):
        _adapter, _out, session_name = live_session
        ok, text = send_command(session_name, "help")
        assert ok is True
        assert "execution:" in text

    def test_send_step_command(self, live_session: Any):
        _adapter, _out, session_name = live_session
        ok, text = send_command(session_name, "step 2")
        assert ok is True
        assert "Stepped" in text
        assert "scan" in text.lower()

    def test_send_force_command(self, live_session: Any):
        adapter, _out, session_name = live_session
        ok, text = send_command(session_name, "force Button true")
        assert ok is True
        assert adapter._runner.forces["Button"] is True

    def test_send_invalid_command(self, live_session: Any):
        _adapter, _out, session_name = live_session
        ok, text = send_command(session_name, "nonexistent_verb")
        assert ok is False
        assert "Unknown command" in text

    def test_send_empty_command(self, live_session: Any):
        _adapter, _out, session_name = live_session
        ok, text = send_command(session_name, "")
        assert ok is False

    def test_multiple_sequential_connections(self, live_session: Any):
        _adapter, _out, session_name = live_session
        for i in range(3):
            ok, text = send_command(session_name, "help")
            assert ok is True, f"connection {i} failed"

    def test_connection_refused_for_bad_session(self):
        with pytest.raises((ConnectionRefusedError, FileNotFoundError)):
            send_command("nonexistent_session_xyz", "help")


# ---------------------------------------------------------------------------
# Session listing
# ---------------------------------------------------------------------------


class TestListSessions:
    def test_list_includes_active_session(self, live_session: Any):
        _adapter, _out, session_name = live_session
        sessions = list_sessions()
        assert session_name in sessions

    def test_list_excludes_stopped_session(self, tmp_path: Path):
        session_name = f"test_list_stop_{id(tmp_path)}"
        adapter, out_stream = _setup_with_session(tmp_path, session_name)
        assert adapter._live_server is not None
        adapter._live_server.stop()
        adapter._live_server = None
        sessions = list_sessions()
        assert session_name not in sessions
