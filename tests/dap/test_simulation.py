"""Tests for DAP simulator controls: mode, speed, frames, address targets, Modbus."""

from __future__ import annotations

import asyncio
import io
import socket
import sys
import threading
import time
from pathlib import Path
from typing import Any

import pytest

from pyrung.dap.adapter import DAPAdapter
from pyrung.dap.execution_flow import PACE_MAX_LAG_SECONDS, ScanPacer, SpeedMeter
from pyrung.dap.live import send_command
from pyrung.dap.modbus import SessionDataProvider
from pyrung.dap.protocol import read_message

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _drain_messages(stream: io.BytesIO) -> list[dict[str, Any]]:
    reader = io.BytesIO(stream.getvalue())
    messages: list[dict[str, Any]] = []
    while (message := read_message(reader)) is not None:
        messages.append(message)
    stream.seek(0)
    stream.truncate(0)
    return messages


def _request(
    adapter: DAPAdapter,
    out_stream: io.BytesIO,
    command: str,
    arguments: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    adapter.handle_request(
        {"seq": 1, "type": "request", "command": command, "arguments": arguments or {}}
    )
    return _drain_messages(out_stream)


def _response(messages: list[dict[str, Any]]) -> dict[str, Any]:
    responses = [m for m in messages if m.get("type") == "response"]
    assert len(responses) == 1
    return responses[0]


def _events(messages: list[dict[str, Any]], name: str) -> list[dict[str, Any]]:
    return [m["body"] for m in messages if m.get("type") == "event" and m.get("event") == name]


def _flush(adapter: DAPAdapter, out_stream: io.BytesIO) -> list[dict[str, Any]]:
    adapter._drain_internal_events()
    return _drain_messages(out_stream)


def _wait_for(
    adapter: DAPAdapter,
    out_stream: io.BytesIO,
    name: str,
    predicate: Any = None,
    timeout: float = 5.0,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    seen: list[dict[str, Any]] = []
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        messages = _flush(adapter, out_stream)
        seen.extend(messages)
        for body in _events(messages, name):
            if predicate is None or predicate(body):
                return body, seen
        time.sleep(0.01)
    raise AssertionError(f"no {name} event matched; saw {[m.get('event') for m in seen]}")


_PROGRAM = """\
from pyrung.core import Bool, Int, PLC, Program, Rung, out, copy

button = Bool("Button")
light = Bool("Light")
level = Int("Level")
latched = Int("Latched")

with Program(strict=False) as prog:
    with Rung(button):
        out(light)
    with Rung():
        copy(level, latched)

runner = PLC(prog, dt=0.010)
"""

_CLICK_PROGRAM = """\
from pyrung.click import ClickBlocks, TagMap
from pyrung.core import Bool, Int, PLC, Program, Rung, out, copy

blocks = ClickBlocks()
x, y, c, t, ct, sc, ds, dd, dh, df, xd, yd, xd0u, yd0u, td, ctd, sd, txt = blocks

start = Bool("Start_PB")
motor = Bool("Motor")
speed = Int("Speed")
stop_pb = x.slot(2, name="Stop_PB")

with Program(strict=False) as prog:
    with Rung(start, ~x[2]):
        out(motor)
    with Rung():
        copy(speed, ds[10])

mapping = TagMap({start: x[1], motor: y[1], speed: ds[1]})
runner = PLC(prog, dt=0.010)
"""


def _launch(
    tmp_path: Path,
    source: str = _PROGRAM,
    *,
    configure: bool = True,
    **launch_args: Any,
) -> tuple[DAPAdapter, io.BytesIO]:
    out_stream = io.BytesIO()
    adapter = DAPAdapter(in_stream=io.BytesIO(), out_stream=out_stream)
    script = tmp_path / "logic.py"
    script.write_text(source, encoding="utf-8")
    messages = _request(
        adapter, out_stream, "launch", {"program": str(script), "session": None, **launch_args}
    )
    assert _response(messages)["success"], messages
    if configure:
        _request(adapter, out_stream, "configurationDone")
        _flush(adapter, out_stream)
    return adapter, out_stream


def _shutdown(adapter: DAPAdapter, out_stream: io.BytesIO) -> None:
    _request(adapter, out_stream, "disconnect")
    _flush(adapter, out_stream)


@pytest.fixture
def session_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    import pyrung.dap.live as live_mod

    directory = tmp_path / "sessions"
    monkeypatch.setattr(live_mod, "_SESSION_DIR", directory)
    return directory


# ---------------------------------------------------------------------------
# Pacing
# ---------------------------------------------------------------------------


class _FakeClock:
    def __init__(self) -> None:
        self.now = 100.0

    def __call__(self) -> float:
        return self.now


def test_pacer_unpaced_never_waits():
    clock = _FakeClock()
    pacer = ScanPacer(clock)
    assert [pacer.delay(0.0, t * 0.01) for t in range(5)] == [0.0] * 5


def test_pacer_keeps_sim_time_at_wall_time_times_speed():
    clock = _FakeClock()
    pacer = ScanPacer(clock)
    assert pacer.delay(2.0, 0.0) == 0.0  # baseline
    assert pacer.delay(2.0, 0.010) == pytest.approx(0.005)
    clock.now += 0.005
    assert pacer.delay(2.0, 0.020) == pytest.approx(0.005)
    # Running late within the lag bound: no wait, and no baseline reset.
    clock.now += 0.020
    assert pacer.delay(2.0, 0.030) == 0.0
    assert pacer.delay(2.0, 0.040) == pytest.approx(0.0)


def test_pacer_does_not_burst_after_falling_far_behind():
    clock = _FakeClock()
    pacer = ScanPacer(clock)
    pacer.delay(1.0, 0.0)
    clock.now += 5.0  # a long stall
    assert pacer.delay(1.0, 0.010) == 0.0
    # The baseline was reset: the next scan waits a full period again
    # instead of running 5 seconds of scans flat out.
    assert pacer.delay(1.0, 0.020) == pytest.approx(0.010)
    assert PACE_MAX_LAG_SECONDS < 5.0


def test_pacer_resets_on_speed_change_and_time_reset():
    clock = _FakeClock()
    pacer = ScanPacer(clock)
    pacer.delay(1.0, 0.0)
    assert pacer.delay(1.0, 0.010) == pytest.approx(0.010)
    assert pacer.delay(4.0, 0.020) == 0.0  # new baseline
    assert pacer.delay(4.0, 0.030) == pytest.approx(0.0025)
    assert pacer.delay(4.0, 0.0) == 0.0  # STOP -> RUN restarted the clock


def test_speed_meter_reports_sim_over_wall_ratio():
    clock = _FakeClock()
    meter = SpeedMeter(clock)
    assert meter.sample(0.0) is None
    clock.now += 0.25
    assert meter.sample(1.0) is None  # window not complete yet
    clock.now += 0.25
    assert meter.sample(3.15) == pytest.approx(6.3)


def test_continue_worker_paces_with_injected_clock(tmp_path: Path):
    adapter, out_stream = _launch(tmp_path, speed=2.0)
    clock = _FakeClock()
    waits: list[float] = []

    def fake_wait(seconds: float) -> bool:
        waits.append(seconds)
        clock.now += seconds
        if len(waits) >= 20:
            adapter._pause_event.set()
        return adapter._pause_event.is_set()

    adapter._clock = clock
    adapter._pace_wait = fake_wait
    _request(adapter, out_stream, "continue")
    _wait_for(adapter, out_stream, "stopped", lambda b: b["reason"] == "pause")

    assert len(waits) >= 19
    # dt = 10 ms at 2x real time: every scan after the baseline waits 5 ms.
    assert waits[1:] == pytest.approx([0.005] * (len(waits) - 1))
    _shutdown(adapter, out_stream)


def test_set_speed_request_and_console_verb(tmp_path: Path):
    adapter, out_stream = _launch(tmp_path)
    assert adapter._speed == 0.0

    messages = _request(adapter, out_stream, "pyrungSetSpeed", {"speed": 1.5})
    assert _response(messages)["body"] == {"speed": 1.5}
    frames = _events(messages, "pyrungScanFrame")
    assert frames[-1]["speed"] == 1.5

    bad = _request(adapter, out_stream, "pyrungSetSpeed", {"speed": -1})
    assert _response(bad)["success"] is False

    messages = _request(adapter, out_stream, "evaluate", {"expression": "speed max"})
    assert _response(messages)["body"]["result"] == "Speed: unpaced"
    assert adapter._speed == 0.0
    messages = _request(adapter, out_stream, "evaluate", {"expression": "speed 3"})
    assert _response(messages)["body"]["result"] == "Speed: 3x real time"
    _shutdown(adapter, out_stream)


# ---------------------------------------------------------------------------
# Mode
# ---------------------------------------------------------------------------


def test_start_mode_stop_and_default(tmp_path: Path):
    adapter, out_stream = _launch(tmp_path, startMode="stop")
    assert adapter._runner is not None and adapter._runner.mode == "stop"
    _shutdown(adapter, out_stream)

    adapter, out_stream = _launch(tmp_path)
    assert adapter._runner is not None and adapter._runner.mode == "run"
    _shutdown(adapter, out_stream)

    out_stream = io.BytesIO()
    adapter = DAPAdapter(in_stream=io.BytesIO(), out_stream=out_stream)
    script = tmp_path / "logic.py"
    messages = _request(
        adapter, out_stream, "launch", {"program": str(script), "startMode": "pause"}
    )
    assert _response(messages)["success"] is False


def test_set_mode_run_applies_forces_and_patches_before_first_scan(tmp_path: Path):
    adapter, out_stream = _launch(tmp_path)
    runner = adapter._runner
    assert runner is not None
    _request(adapter, out_stream, "pyrungForce", {"tag": "Latched", "value": 9})

    messages = _request(adapter, out_stream, "pyrungSetMode", {"mode": "stop"})
    assert _response(messages)["body"]["mode"] == "stop"
    assert _events(messages, "pyrungScanFrame")[-1]["mode"] == "stop"
    assert runner.mode == "stop"

    messages = _request(
        adapter,
        out_stream,
        "pyrungSetMode",
        {"mode": "run", "forces": {"Button": True}, "patches": {"Level": 42}},
    )
    body = _response(messages)["body"]
    # STOP -> RUN cleared the old force before the new ones were applied.
    assert body == {"mode": "run", "forces": {"Button": True}, "pendingPatches": {"Level": 42}}
    frame = _events(messages, "pyrungScanFrame")[-1]
    assert frame["mode"] == "run"
    assert frame["running"] is False
    assert frame["trace"]["forces"] == {"Button": True}
    assert frame["pendingPatches"] == {"Level": 42}
    # No scan ran: the transition reset scan_id without executing logic.
    assert runner.current_state.scan_id == 0
    assert runner.current_state.tags.get("Light", False) is False

    _request(adapter, out_stream, "pyrungStepScan")
    tags = runner.current_state.tags
    assert runner.current_state.scan_id == 1
    assert tags["Light"] is True
    assert tags["Latched"] == 42
    _shutdown(adapter, out_stream)


def test_keep_forces_on_run_launch_argument(tmp_path: Path):
    adapter, out_stream = _launch(tmp_path, keepForcesOnRun=True)
    runner = adapter._runner
    assert runner is not None and runner.keep_forces_on_run is True
    _request(adapter, out_stream, "pyrungForce", {"tag": "Button", "value": True})
    _request(adapter, out_stream, "pyrungSetMode", {"mode": "stop"})
    messages = _request(
        adapter, out_stream, "pyrungSetMode", {"mode": "run", "forces": {"Level": 2}}
    )
    assert _response(messages)["body"]["forces"] == {"Button": True, "Level": 2}

    # Reload keeps the launch setting.
    _request(adapter, out_stream, "evaluate", {"expression": "reload"})
    assert adapter._runner is not None and adapter._runner.keep_forces_on_run is True
    _shutdown(adapter, out_stream)

    adapter, out_stream = _launch(tmp_path)
    assert adapter._runner is not None and adapter._runner.keep_forces_on_run is False
    _shutdown(adapter, out_stream)


def test_set_mode_pauses_running_executor(tmp_path: Path):
    adapter, out_stream = _launch(tmp_path)
    _request(adapter, out_stream, "continue")
    _wait_for(adapter, out_stream, "pyrungScanFrame", lambda b: b["running"] is True)

    messages = _request(adapter, out_stream, "pyrungSetMode", {"mode": "stop"})
    assert _response(messages)["success"] is True
    assert adapter._continue_thread is None
    assert adapter._runner is not None and adapter._runner.mode == "stop"
    _shutdown(adapter, out_stream)


def test_set_mode_rejects_bad_mode(tmp_path: Path):
    adapter, out_stream = _launch(tmp_path)
    messages = _request(adapter, out_stream, "pyrungSetMode", {"mode": "program"})
    assert _response(messages)["success"] is False
    _shutdown(adapter, out_stream)


def test_mode_console_verb(tmp_path: Path):
    adapter, out_stream = _launch(tmp_path)
    messages = _request(adapter, out_stream, "evaluate", {"expression": "mode stop"})
    assert _response(messages)["body"]["result"] == "Mode: STOP"
    frame = _events(messages, "pyrungScanFrame")[-1]
    assert frame["mode"] == "stop"
    assert frame["source"] == {"origin": "dap", "command": "mode stop"}
    messages = _request(adapter, out_stream, "evaluate", {"expression": "mode"})
    assert _response(messages)["body"]["result"] == "Mode: STOP"
    messages = _request(adapter, out_stream, "evaluate", {"expression": "mode run"})
    assert _response(messages)["body"]["result"] == "Mode: RUN"
    _shutdown(adapter, out_stream)


def test_step_while_continuing_pauses_then_steps(tmp_path: Path):
    adapter, out_stream = _launch(tmp_path)
    _request(adapter, out_stream, "continue")
    _wait_for(adapter, out_stream, "pyrungScanFrame", lambda b: b["running"] is True)

    messages = _request(adapter, out_stream, "evaluate", {"expression": "step 3"})
    assert _response(messages)["success"] is True
    assert adapter._continue_thread is None
    assert _events(messages, "pyrungScanFrame")[-1]["running"] is False

    _request(adapter, out_stream, "continue")
    _wait_for(adapter, out_stream, "pyrungScanFrame", lambda b: b["running"] is True)
    messages = _request(adapter, out_stream, "pyrungStepScan")
    assert _response(messages)["success"] is True
    _shutdown(adapter, out_stream)


# ---------------------------------------------------------------------------
# Address targets
# ---------------------------------------------------------------------------


def test_address_targets_resolve_through_tag_map(tmp_path: Path):
    adapter, out_stream = _launch(tmp_path, _CLICK_PROGRAM)
    runner = adapter._runner
    assert runner is not None

    _request(adapter, out_stream, "pyrungForce", {"tag": "X001", "value": True})
    # A slot named on the Click bank itself (blocks, not the TagMap).
    _request(adapter, out_stream, "pyrungForce", {"tag": "x2", "value": False})
    assert dict(runner.forces) == {"Start_PB": True, "Stop_PB": False}

    _request(adapter, out_stream, "pyrungPatch", {"patches": {"DS1": 7, "Motor": True}})
    assert dict(runner.pending_patches) == {"Speed": 7, "Motor": True}

    messages = _request(adapter, out_stream, "pyrungUnforce", {"tag": "X001"})
    assert _response(messages)["success"] is True
    assert dict(runner.forces) == {"Stop_PB": False}

    # A tag name that looks like an address wins over the address.
    messages = _request(adapter, out_stream, "pyrungPatch", {"tag": "DS10", "value": 3})
    assert _response(messages)["success"] is True
    assert runner.pending_patches["DS10"] == 3
    _shutdown(adapter, out_stream)


def test_unknown_targets_are_errors(tmp_path: Path):
    adapter, out_stream = _launch(tmp_path, _CLICK_PROGRAM)
    runner = adapter._runner
    assert runner is not None

    # Timer/counter banks are only reachable through TagMap entries.
    messages = _request(adapter, out_stream, "pyrungForce", {"tag": "td5", "value": 1})
    response = _response(messages)
    assert response["success"] is False
    assert response["message"] == "unknown tag: td5 (address TD5 is not mapped to a tag)"

    messages = _request(adapter, out_stream, "pyrungPatch", {"tag": "Nope", "value": 1})
    assert _response(messages)["message"] == "unknown tag: Nope"

    messages = _request(
        adapter, out_stream, "pyrungSetMode", {"mode": "run", "forces": {"CTD3": 1}}
    )
    assert _response(messages)["success"] is False

    messages = _request(adapter, out_stream, "evaluate", {"expression": "force Nope 1"})
    assert _response(messages)["success"] is False
    messages = _request(adapter, out_stream, "evaluate", {"expression": "force X001 1"})
    assert _response(messages)["success"] is True
    assert dict(runner.forces) == {"Start_PB": 1}
    # A plain-bank address the program never uses is still PLC memory.
    messages = _request(adapter, out_stream, "pyrungForce", {"tag": "C5", "value": True})
    assert _response(messages)["success"] is True
    assert runner.forces["C5"] is True
    _shutdown(adapter, out_stream)


def test_addresses_without_tag_map_are_errors(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    # Discovery also accepts a top-level ``tags`` module; other tests may leave one.
    monkeypatch.delitem(sys.modules, "tags", raising=False)
    adapter, out_stream = _launch(tmp_path)
    messages = _request(adapter, out_stream, "pyrungForce", {"tag": "C5", "value": True})
    assert _response(messages)["message"] == (
        "unknown tag: C5 (address C5 is not mapped to a tag; the project has no TagMap)"
    )
    _shutdown(adapter, out_stream)


# ---------------------------------------------------------------------------
# Patches are never lost between scans
# ---------------------------------------------------------------------------


def test_patch_after_console_step_lands_in_the_next_step_scan(tmp_path: Path):
    # `step N` stops on the first rung of the following scan, whose prepare
    # phase already ran.  A patch sent then must still reach the next scan.
    adapter, out_stream = _launch(tmp_path)
    runner = adapter._runner
    assert runner is not None
    _request(adapter, out_stream, "evaluate", {"expression": "step 5"})

    _request(adapter, out_stream, "pyrungPatch", {"tag": "Level", "value": 7})
    messages = _request(adapter, out_stream, "pyrungStepScan")

    frame = _events(messages, "pyrungScanFrame")[-1]
    assert frame["pendingPatches"] == {}
    assert runner.current_state.tags["Level"] == 7
    assert runner.current_state.tags["Latched"] == 7
    _shutdown(adapter, out_stream)


def test_patch_drained_by_an_abandoned_scan_is_queued_again(tmp_path: Path):
    adapter, out_stream = _launch(tmp_path)
    runner = adapter._runner
    assert runner is not None
    _request(adapter, out_stream, "pyrungPatch", {"tag": "Level", "value": 4})

    # One rung into the scan: the patch is taken but not yet committed.
    messages = _request(adapter, out_stream, "next")
    assert dict(runner.pending_patches) == {"Level": 4}
    assert _events(messages, "pyrungScanFrame")[-1]["pendingPatches"] == {"Level": 4}

    # A console force restarts the partial scan; the patch must survive that.
    _request(adapter, out_stream, "evaluate", {"expression": "force Button true"})
    assert dict(runner.pending_patches) == {"Level": 4}

    messages = _request(adapter, out_stream, "pyrungStepScan")
    assert runner.current_state.tags["Latched"] == 4
    assert runner.current_state.tags["Light"] is True
    assert _events(messages, "pyrungScanFrame")[-1]["pendingPatches"] == {}
    _shutdown(adapter, out_stream)


def test_modbus_write_while_paused_lands_in_the_next_step_scan(tmp_path: Path):
    adapter, out_stream = _launch(tmp_path, _CLICK_PROGRAM)
    runner = adapter._runner
    assert runner is not None
    provider = SessionDataProvider(adapter)
    _request(adapter, out_stream, "evaluate", {"expression": "step 5"})

    provider.write("DS1", 13)
    _request(adapter, out_stream, "pyrungStepScan")
    assert runner.current_state.tags["Speed"] == 13
    assert provider.read("DS10") == 13
    _shutdown(adapter, out_stream)


def test_lock_free_requests_do_not_wait_for_the_state_lock(tmp_path: Path):
    adapter, out_stream = _launch(tmp_path)
    for command in ("pause", "threads", "pyrungModbusInfo"):
        done = threading.Event()

        def send(command: str = command, done: threading.Event = done) -> None:
            adapter.handle_request(
                {"seq": 9, "type": "request", "command": command, "arguments": {}}
            )
            done.set()

        with adapter._state_lock:  # e.g. a long `how` from pyrung live
            worker = threading.Thread(target=send)
            worker.start()
            assert done.wait(timeout=1.0), f"{command} waited for the state lock"
        worker.join(timeout=1.0)
    _flush(adapter, out_stream)
    _shutdown(adapter, out_stream)


# ---------------------------------------------------------------------------
# Frames
# ---------------------------------------------------------------------------


def test_frames_follow_dap_state_changes(tmp_path: Path):
    adapter, out_stream = _launch(tmp_path, configure=False)
    messages = _request(adapter, out_stream, "configurationDone")
    messages += _flush(adapter, out_stream)
    frame = _events(messages, "pyrungScanFrame")[-1]
    assert frame["source"] == {"origin": "dap", "command": "configurationDone"}
    assert frame["mode"] == "run"
    assert frame["running"] is False
    assert frame["speed"] == 0.0
    assert frame["actualSpeed"] is None
    assert frame["pendingPatches"] == {}
    assert frame["trace"]["forces"] == {}

    messages = _request(adapter, out_stream, "pyrungForce", {"tag": "Button", "value": True})
    frame = _events(messages, "pyrungScanFrame")[-1]
    assert frame["trace"]["forces"] == {"Button": True}
    assert frame["source"] == {"origin": "dap", "command": "pyrungForce"}

    messages = _request(adapter, out_stream, "pyrungPatch", {"tag": "Level", "value": 5})
    assert _events(messages, "pyrungScanFrame")[-1]["pendingPatches"] == {"Level": 5}

    messages = _request(adapter, out_stream, "pyrungStepScan")
    frame = _events(messages, "pyrungScanFrame")[-1]
    assert frame["pendingPatches"] == {}
    assert frame["trace"]["tagValues"]["Latched"] == "5"

    messages = _request(adapter, out_stream, "pyrungClearForces")
    assert _events(messages, "pyrungScanFrame")[-1]["trace"]["forces"] == {}

    # Queries do not emit frames.
    messages = _request(adapter, out_stream, "pyrungListForces")
    assert _events(messages, "pyrungScanFrame") == []
    _shutdown(adapter, out_stream)


def test_worker_frames_report_running_and_source(tmp_path: Path):
    adapter, out_stream = _launch(tmp_path)
    _request(adapter, out_stream, "continue")
    frame, _ = _wait_for(
        adapter,
        out_stream,
        "pyrungScanFrame",
        lambda b: (b["source"] or {}).get("origin") == "worker",
    )
    assert frame["running"] is True
    assert frame["source"] == {"origin": "worker", "command": "continue"}
    _request(adapter, out_stream, "pause")
    final, _ = _wait_for(
        adapter,
        out_stream,
        "pyrungScanFrame",
        lambda b: b["running"] is False and (b["source"] or {}).get("origin") == "worker",
    )
    assert final["actualSpeed"] is None
    _shutdown(adapter, out_stream)


def test_live_commands_emit_events_and_frames(tmp_path: Path, session_dir: Path):
    out_stream = io.BytesIO()
    adapter = DAPAdapter(in_stream=io.BytesIO(), out_stream=out_stream)
    script = tmp_path / "logic.py"
    script.write_text(_PROGRAM, encoding="utf-8")
    _request(adapter, out_stream, "launch", {"program": str(script), "session": "simlive"})
    _request(adapter, out_stream, "configurationDone")
    _flush(adapter, out_stream)

    ok, text = send_command("simlive", "force Button true")
    assert ok, text
    messages = _flush(adapter, out_stream)
    frame = _events(messages, "pyrungScanFrame")[-1]
    assert frame["trace"]["forces"] == {"Button": True}
    assert frame["source"] == {"origin": "live", "command": "force Button true"}

    ok, text = send_command("simlive", "step 2")
    assert ok, text
    messages = _flush(adapter, out_stream)
    assert [b["reason"] for b in _events(messages, "stopped")] == ["step"]
    assert len(_events(messages, "pyrungTrace")) == 1
    frame = _events(messages, "pyrungScanFrame")[-1]
    assert frame["trace"]["tagValues"]["Light"] == "True"
    assert frame["source"] == {"origin": "live", "command": "step 2"}

    ok, text = send_command("simlive", "get Light")
    assert ok, text
    assert _events(_flush(adapter, out_stream), "pyrungScanFrame") == []

    ok, text = send_command("simlive", "force Bogus 1")
    assert not ok
    assert "unknown tag: Bogus" in text
    _shutdown(adapter, out_stream)


def test_live_step_while_continuing_pauses_first(tmp_path: Path, session_dir: Path):
    out_stream = io.BytesIO()
    adapter = DAPAdapter(in_stream=io.BytesIO(), out_stream=out_stream)
    script = tmp_path / "logic.py"
    script.write_text(_PROGRAM, encoding="utf-8")
    _request(adapter, out_stream, "launch", {"program": str(script), "session": "simstep"})
    _request(adapter, out_stream, "configurationDone")
    _request(adapter, out_stream, "continue")
    _wait_for(adapter, out_stream, "pyrungScanFrame", lambda b: b["running"] is True)

    ok, text = send_command("simstep", "step 1")
    assert ok, text
    assert adapter._continue_thread is None
    _shutdown(adapter, out_stream)


# ---------------------------------------------------------------------------
# Modbus
# ---------------------------------------------------------------------------


def _modbus_launch(tmp_path: Path, **modbus: Any) -> tuple[DAPAdapter, io.BytesIO]:
    config = {"host": "127.0.0.1", "port": 0, **modbus}
    return _launch(tmp_path, _CLICK_PROGRAM, modbus=config)


def test_modbus_info_without_server(tmp_path: Path):
    adapter, out_stream = _launch(tmp_path)
    messages = _request(adapter, out_stream, "pyrungModbusInfo")
    assert _response(messages)["body"] == {
        "running": False,
        "host": None,
        "port": None,
        "clients": 0,
        "error": None,
    }
    _shutdown(adapter, out_stream)


def test_modbus_server_round_trip_and_stop(tmp_path: Path):
    from pyclickplc import ClickClient

    adapter, out_stream = _modbus_launch(tmp_path)
    info = _response(_request(adapter, out_stream, "pyrungModbusInfo"))["body"]
    assert info["running"] is True
    assert info["error"] is None
    port = info["port"]
    assert port > 0

    runner = adapter._runner
    assert runner is not None
    _request(adapter, out_stream, "pyrungPatch", {"tag": "Speed", "value": 11})
    _request(adapter, out_stream, "pyrungStepScan")

    async def exchange() -> Any:
        async with ClickClient("127.0.0.1", port) as plc:
            value = await plc.addr.read("DS1")
            await plc.addr.write("DS1", 25)
            return value

    value = asyncio.run(exchange())
    assert dict(value) == {"DS1": 11}
    assert dict(runner.pending_patches) == {"Speed": 25}
    frame, _ = _wait_for(adapter, out_stream, "pyrungScanFrame")
    assert frame["source"] == {"origin": "modbus", "command": "write DS1"}
    assert frame["pendingPatches"] == {"Speed": 25}

    _shutdown(adapter, out_stream)
    assert adapter._modbus is None
    # The port is free again.
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", port))


def test_modbus_bind_failure_does_not_fail_launch(tmp_path: Path):
    with socket.socket() as blocker:
        blocker.bind(("127.0.0.1", 0))
        blocker.listen()
        port = blocker.getsockname()[1]
        out_stream = io.BytesIO()
        adapter = DAPAdapter(in_stream=io.BytesIO(), out_stream=out_stream)
        script = tmp_path / "logic.py"
        script.write_text(_CLICK_PROGRAM, encoding="utf-8")
        messages = _request(
            adapter,
            out_stream,
            "launch",
            {"program": str(script), "modbus": {"host": "127.0.0.1", "port": port}},
        )
        assert _response(messages)["success"] is True
        outputs = [b["output"] for b in _events(_flush(adapter, out_stream), "output")]
        assert any("Modbus TCP server not started" in text for text in outputs)

        info = _response(_request(adapter, out_stream, "pyrungModbusInfo"))["body"]
        assert info["running"] is False
        assert info["port"] == port
        assert info["error"].startswith(f"Cannot listen on 127.0.0.1:{port}")
        _shutdown(adapter, out_stream)


def test_modbus_provider_writes_hold_the_state_lock(tmp_path: Path):
    adapter, out_stream = _launch(tmp_path, _CLICK_PROGRAM)
    provider = SessionDataProvider(adapter)
    runner = adapter._runner
    assert runner is not None

    with adapter._state_lock:
        writer = threading.Thread(target=provider.write, args=("DS1", 5))
        writer.start()
        writer.join(timeout=0.2)
        assert writer.is_alive(), "write must wait for the state lock"
        assert dict(runner.pending_patches) == {}
    writer.join(timeout=2.0)
    assert dict(runner.pending_patches) == {"Speed": 5}
    assert provider.read("Y001") is False
    _shutdown(adapter, out_stream)


def test_modbus_provider_follows_reloaded_runner(tmp_path: Path):
    adapter, out_stream = _launch(tmp_path, _CLICK_PROGRAM)
    provider = SessionDataProvider(adapter)
    first = adapter._runner
    messages = _request(adapter, out_stream, "evaluate", {"expression": "reload"})
    assert _response(messages)["success"] is True
    assert adapter._runner is not first
    provider.write("DS1", 8)
    assert adapter._runner is not None
    assert dict(adapter._runner.pending_patches) == {"Speed": 8}
    _shutdown(adapter, out_stream)


def test_modbus_stop_command_pauses_and_enters_stop(tmp_path: Path):
    adapter, out_stream = _launch(tmp_path, _CLICK_PROGRAM)
    provider = SessionDataProvider(adapter)
    _request(adapter, out_stream, "continue")
    _wait_for(adapter, out_stream, "pyrungScanFrame", lambda b: b["running"] is True)

    provider.write("SC50", True)
    _, seen = _wait_for(adapter, out_stream, "stopped", lambda b: b["reason"] == "pause")
    frames = [f for f in _events(seen, "pyrungScanFrame") if f["mode"] == "stop"]
    assert frames
    assert frames[-1]["running"] is False
    assert adapter._runner is not None and adapter._runner.mode == "stop"
    _shutdown(adapter, out_stream)
