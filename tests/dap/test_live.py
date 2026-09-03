"""Tests for the pyrung-live server and client."""

from __future__ import annotations

import io
import threading
import time
from pathlib import Path
from typing import Any

import pytest

from pyrung.dap.adapter import DAPAdapter
from pyrung.dap.cancel import CancelToken, ConsoleCancelled
from pyrung.dap.live import _build_command_epilog, list_sessions, send_command
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


@pytest.fixture()
def slow_verb():
    """Register a `slowtest` verb that blocks under the state lock until cancelled.

    Stands in for `how` -- it polls the cancel token the same way, without
    needing a program big enough for the planner to take a measurable while.
    """
    from pyrung.dap import console as console_mod

    started = threading.Event()

    @console_mod.register("slowtest", usage="slowtest", group="")
    def _cmd_slowtest(adapter: Any, _expression: str) -> console_mod.ConsoleResult:
        started.set()
        deadline = time.monotonic() + 10.0
        while time.monotonic() < deadline:
            adapter._cancel.check("slowtest")
            time.sleep(0.01)
        return console_mod.ConsoleResult("completed")

    yield started
    console_mod._REGISTRY.pop("slowtest", None)


# ---------------------------------------------------------------------------
# Server lifecycle
# ---------------------------------------------------------------------------


def test_cli_help_includes_review_commands():
    help_text = _build_command_epilog()
    assert "review:" in help_text
    assert "candidates [list]" in help_text
    assert "spec [list] | spec test <filepath>" in help_text


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
# Cooperative cancellation
# ---------------------------------------------------------------------------


class TestCancelToken:
    def test_check_is_quiet_until_requested(self):
        token = CancelToken()
        token.check()
        assert token.requested is False

    def test_check_raises_after_request(self):
        token = CancelToken()
        token.request()
        assert token.requested is True
        with pytest.raises(ConsoleCancelled, match="how cancelled"):
            token.check("how")

    def test_reset_clears_the_flag(self):
        token = CancelToken()
        token.request()
        token.reset()
        assert token.requested is False
        token.check()


class TestStopCommand:
    def test_stop_with_nothing_running(self, live_session: Any):
        _adapter, _out, session_name = live_session
        ok, text = send_command(session_name, "stop")
        assert ok is True
        assert text == "Nothing running."

    def test_stop_cancels_inflight_command(self, live_session: Any, slow_verb: threading.Event):
        _adapter, _out, session_name = live_session
        result: dict[str, Any] = {}

        def _run() -> None:
            ok, text = send_command(session_name, "slowtest")
            result["ok"], result["text"] = ok, text

        worker = threading.Thread(target=_run, daemon=True)
        worker.start()
        assert slow_verb.wait(5.0), "slowtest never started"

        # The command now holds _state_lock. This must still be answered.
        ok, text = send_command(session_name, "stop")
        assert ok is True
        assert "Stop requested" in text
        assert "slowtest" in text

        worker.join(5.0)
        assert not worker.is_alive(), "cancelled command did not unwind"
        assert result["ok"] is False
        assert "slowtest cancelled" in result["text"]

    def test_session_usable_after_cancel(self, live_session: Any, slow_verb: threading.Event):
        _adapter, _out, session_name = live_session

        worker = threading.Thread(
            target=lambda: send_command(session_name, "slowtest"), daemon=True
        )
        worker.start()
        assert slow_verb.wait(5.0)
        send_command(session_name, "stop")
        worker.join(5.0)
        assert not worker.is_alive()

        # The stale stop flag must not poison the next command.
        ok, text = send_command(session_name, "step 1")
        assert ok is True, text
        assert "Stepped" in text

    def test_inflight_cleared_after_normal_completion(self, live_session: Any):
        _adapter, _out, session_name = live_session
        ok, _text = send_command(session_name, "step 1")
        assert ok is True
        ok, text = send_command(session_name, "stop")
        assert text == "Nothing running."


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
