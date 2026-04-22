"""Tests for DAP harness integration (auto-install, console verbs, capture provenance)."""

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


def _single_response(messages: list[dict[str, Any]]) -> dict[str, Any]:
    responses = [msg for msg in messages if msg.get("type") == "response"]
    assert len(responses) == 1
    return responses[0]


def _output_events(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [msg for msg in messages if msg.get("type") == "event" and msg.get("event") == "output"]


def _write_script(tmp_path: Path, name: str, content: str) -> Path:
    script_path = tmp_path / name
    script_path.write_text(content, encoding="utf-8")
    return script_path


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
    all_msgs = messages
    return response, all_msgs


# ---------------------------------------------------------------------------
# Scripts
# ---------------------------------------------------------------------------


def _harness_script() -> str:
    return (
        "from pyrung.core import Bool, Field, PLC, Program, Rung, out, udt\n"
        "from pyrung.core.physical import Physical\n"
        "\n"
        "SWITCH = Physical('Switch', on_delay='20ms', off_delay='10ms')\n"
        "\n"
        "@udt()\n"
        "class Device:\n"
        "    En: Bool\n"
        "    Fb: Bool = Field(physical=SWITCH, link='En')\n"
        "\n"
        "Cmd = Bool('Cmd')\n"
        "with Program() as prog:\n"
        "    with Rung(Cmd):\n"
        "        out(Device[1].En)\n"
        "\n"
        "runner = PLC(prog, dt=0.010)\n"
    )


def _plain_script() -> str:
    return (
        "from pyrung.core import Bool, PLC, Program, Rung, out\n"
        "\n"
        "button = Bool('Button')\n"
        "light = Bool('Light')\n"
        "\n"
        "with Program() as prog:\n"
        "    with Rung(button):\n"
        "        out(light)\n"
        "\n"
        "runner = PLC(prog, dt=0.010)\n"
    )


# ---------------------------------------------------------------------------
# Setup helpers
# ---------------------------------------------------------------------------


def _setup_with_harness(tmp_path: Path) -> tuple[DAPAdapter, io.BytesIO]:
    out_stream = io.BytesIO()
    adapter = DAPAdapter(in_stream=io.BytesIO(), out_stream=out_stream)
    script = _write_script(tmp_path, "logic.py", _harness_script())
    _send_request(adapter, out_stream, seq=1, command="launch", arguments={"program": str(script)})
    _send_request(adapter, out_stream, seq=2, command="configurationDone")
    _drain_messages(out_stream)
    _send_request(adapter, out_stream, seq=3, command="next")
    _drain_messages(out_stream)
    return adapter, out_stream


def _setup_plain(tmp_path: Path) -> tuple[DAPAdapter, io.BytesIO]:
    out_stream = io.BytesIO()
    adapter = DAPAdapter(in_stream=io.BytesIO(), out_stream=out_stream)
    script = _write_script(tmp_path, "logic.py", _plain_script())
    _send_request(adapter, out_stream, seq=1, command="launch", arguments={"program": str(script)})
    _send_request(adapter, out_stream, seq=2, command="configurationDone")
    _drain_messages(out_stream)
    _send_request(adapter, out_stream, seq=3, command="next")
    _drain_messages(out_stream)
    return adapter, out_stream


# ---------------------------------------------------------------------------
# Auto-install
# ---------------------------------------------------------------------------


class TestAutoInstall:
    def test_auto_installs_when_annotations_present(self, tmp_path: Path):
        adapter, _out = _setup_with_harness(tmp_path)
        assert adapter._harness is not None
        assert adapter._harness._installed is True

    def test_no_install_without_annotations(self, tmp_path: Path):
        adapter, _out = _setup_plain(tmp_path)
        assert adapter._harness is None

    def test_banner_emitted_on_auto_install(self, tmp_path: Path):
        out_stream = io.BytesIO()
        adapter = DAPAdapter(in_stream=io.BytesIO(), out_stream=out_stream)
        script = _write_script(tmp_path, "logic.py", _harness_script())
        msgs = _send_request(
            adapter, out_stream, seq=1, command="launch", arguments={"program": str(script)}
        )
        adapter._drain_internal_events()
        all_msgs = _drain_messages(out_stream)
        outputs = _output_events(msgs + all_msgs)
        banner_texts = [
            o["body"]["output"]
            for o in outputs
            if "Harness:" in o.get("body", {}).get("output", "")
        ]
        assert any("feedback loop" in t for t in banner_texts)


# ---------------------------------------------------------------------------
# Console verbs
# ---------------------------------------------------------------------------


class TestHarnessStatus:
    def test_status_when_installed(self, tmp_path: Path):
        adapter, out = _setup_with_harness(tmp_path)
        resp, _ = _repl(adapter, out, "harness status")
        assert resp["success"] is True
        result = resp["body"]["result"]
        assert "active" in result
        assert "bool" in result

    def test_status_when_not_installed(self, tmp_path: Path):
        adapter, out = _setup_plain(tmp_path)
        resp, _ = _repl(adapter, out, "harness status")
        assert resp["success"] is True
        assert "not installed" in resp["body"]["result"]


class TestHarnessRemove:
    def test_remove(self, tmp_path: Path):
        adapter, out = _setup_with_harness(tmp_path)
        assert adapter._harness is not None
        resp, _ = _repl(adapter, out, "harness remove")
        assert resp["success"] is True
        assert "removed" in resp["body"]["result"].lower()
        assert adapter._harness is None

    def test_remove_when_not_installed(self, tmp_path: Path):
        adapter, out = _setup_plain(tmp_path)
        resp, _ = _repl(adapter, out, "harness remove")
        assert resp["success"] is True
        assert "not installed" in resp["body"]["result"].lower()


class TestHarnessInstall:
    def test_install_after_remove(self, tmp_path: Path):
        adapter, out = _setup_with_harness(tmp_path)
        _repl(adapter, out, "harness remove", seq=10)
        assert adapter._harness is None
        resp, _ = _repl(adapter, out, "harness install", seq=11)
        assert resp["success"] is True
        assert "feedback loop" in resp["body"]["result"]
        assert adapter._harness is not None

    def test_install_when_already_installed(self, tmp_path: Path):
        adapter, out = _setup_with_harness(tmp_path)
        resp, _ = _repl(adapter, out, "harness install")
        assert resp["success"] is True
        assert "already installed" in resp["body"]["result"].lower()

    def test_install_with_no_annotations(self, tmp_path: Path):
        adapter, out = _setup_plain(tmp_path)
        resp, _ = _repl(adapter, out, "harness install")
        assert resp["success"] is True
        assert "nothing to install" in resp["body"]["result"].lower()


class TestHarnessErrors:
    def test_missing_subcommand(self, tmp_path: Path):
        adapter, out = _setup_with_harness(tmp_path)
        resp, _ = _repl(adapter, out, "harness")
        assert resp["success"] is False

    def test_unknown_subcommand(self, tmp_path: Path):
        adapter, out = _setup_with_harness(tmp_path)
        resp, _ = _repl(adapter, out, "harness foobar")
        assert resp["success"] is False


# ---------------------------------------------------------------------------
# Patch visibility and capture provenance
# ---------------------------------------------------------------------------


class TestHarnessPatches:
    def test_harness_patches_arrive(self, tmp_path: Path):
        adapter, out = _setup_with_harness(tmp_path)
        _repl(adapter, out, "patch Cmd true", seq=10)
        _repl(adapter, out, "run 50ms", seq=11)
        fb = adapter._runner.current_state.tags.get("Device_Fb", False)
        assert fb is True

    def test_capture_records_harness_provenance(self, tmp_path: Path):
        adapter, out = _setup_with_harness(tmp_path)
        _repl(adapter, out, "record test_harness", seq=10)
        _repl(adapter, out, "patch Cmd true", seq=11)
        _repl(adapter, out, "run 50ms", seq=12)
        resp, _ = _repl(adapter, out, "record stop", seq=15)
        assert resp["success"] is True
        transcript = resp["body"]["result"]
        assert "harness:nominal" in transcript

    def test_harness_patches_as_comments_in_transcript(self, tmp_path: Path):
        adapter, out = _setup_with_harness(tmp_path)
        _repl(adapter, out, "record test_harness", seq=10)
        _repl(adapter, out, "patch Cmd true", seq=11)
        _repl(adapter, out, "run 50ms", seq=12)
        resp, _ = _repl(adapter, out, "record stop", seq=20)
        transcript = resp["body"]["result"]
        for line in transcript.splitlines():
            if "harness:" in line:
                assert line.startswith("# harness:")

    def test_uninstall_on_disconnect(self, tmp_path: Path):
        adapter, out = _setup_with_harness(tmp_path)
        assert adapter._harness is not None
        _send_request(adapter, out, seq=99, command="disconnect")
        assert adapter._harness is None
