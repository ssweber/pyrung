"""Tests for record-and-replay of send/receive I/O instructions."""

from __future__ import annotations

from concurrent.futures import Future

import pytest

from pyrung.core import PLC, Bool, Int, Program, Rung, out
from pyrung.core.instruction.send_receive import ModbusTcpTarget
from pyrung.core.instruction.send_receive import _core as _sr
from pyrung.core.scan_log import IoResultRecord, IoSubmitRecord

_TARGET = ModbusTcpTarget("test", "127.0.0.1", port=502, device_id=1)


def _make_send_program():
    Enable = Bool("Enable")
    Source = Int("Source")
    Sending = Bool("Sending")
    Success = Bool("Success")
    Error = Bool("Error")
    ExCode = Int("ExCode")
    with Program(strict=False) as logic:
        with Rung(Enable):
            _sr.send(
                target=_TARGET,
                remote_start="DS1",
                source=Source,
                sending=Sending,
                success=Success,
                error=Error,
                exception_response=ExCode,
            )
    return logic


def _make_receive_program():
    Enable = Bool("Enable")
    Dest = Int("Dest")
    Receiving = Bool("Receiving")
    Success = Bool("Success")
    Error = Bool("Error")
    ExCode = Int("ExCode")
    with Program(strict=False) as logic:
        with Rung(Enable):
            _sr.receive(
                target=_TARGET,
                remote_start="DS1",
                dest=Dest,
                receiving=Receiving,
                success=Success,
                error=Error,
                exception_response=ExCode,
            )
    return logic


def _fake_send_submit(monkeypatch):
    futures: list[Future[_sr._RequestResult]] = []

    def fake(**kwargs):
        fut: Future[_sr._RequestResult] = Future()
        futures.append(fut)
        return fut

    monkeypatch.setattr(_sr, "_submit_click_send_request", fake)
    return futures


def _fake_recv_submit(monkeypatch):
    futures: list[Future[_sr._RequestResult]] = []

    def fake(**kwargs):
        fut: Future[_sr._RequestResult] = Future()
        futures.append(fut)
        return fut

    monkeypatch.setattr(_sr, "_submit_click_receive_request", fake)
    return futures


# ---------------------------------------------------------------------------
# Recording tests
# ---------------------------------------------------------------------------


def test_send_submit_drain_recorded_in_scan_log(monkeypatch: pytest.MonkeyPatch):
    futures = _fake_send_submit(monkeypatch)

    logic = _make_send_program()
    runner = PLC(logic=logic)
    runner.patch({"Enable": True, "Source": 42})
    runner.step()  # scan 1: submit

    log = runner._scan_log.snapshot()
    assert 1 in log.io_submits_by_scan
    submit_key = "send:test:Sending"
    assert submit_key in log.io_submits_by_scan[1]
    rec = log.io_submits_by_scan[1][submit_key]
    assert isinstance(rec, IoSubmitRecord)
    writes = dict(rec.tag_writes)
    assert writes["Sending"] is True

    futures[0].set_result(_sr._RequestResult(ok=True, exception_code=0))
    runner.patch({"Enable": True})
    runner.step()  # scan 2: drain

    log = runner._scan_log.snapshot()
    assert 2 in log.io_drains_by_scan
    assert submit_key in log.io_drains_by_scan[2]
    drain_rec = log.io_drains_by_scan[2][submit_key]
    assert isinstance(drain_rec, IoResultRecord)
    assert drain_rec.ok is True
    drain_writes = dict(drain_rec.tag_writes)
    assert drain_writes["Sending"] is False
    assert drain_writes["Success"] is True


def test_receive_submit_drain_recorded_in_scan_log(monkeypatch: pytest.MonkeyPatch):
    futures = _fake_recv_submit(monkeypatch)

    logic = _make_receive_program()
    runner = PLC(logic=logic)
    runner.patch({"Enable": True})
    runner.step()  # scan 1: submit

    log = runner._scan_log.snapshot()
    recv_key = "recv:test:Receiving"
    assert recv_key in log.io_submits_by_scan.get(1, {})

    futures[0].set_result(_sr._RequestResult(ok=True, exception_code=0, values=(999,)))
    runner.patch({"Enable": True})
    runner.step()  # scan 2: drain

    log = runner._scan_log.snapshot()
    assert recv_key in log.io_drains_by_scan.get(2, {})
    drain_rec = log.io_drains_by_scan[2][recv_key]
    assert drain_rec.ok is True
    assert drain_rec.values == (999,)
    drain_writes = dict(drain_rec.tag_writes)
    assert drain_writes["Dest"] == 999
    assert drain_writes["Success"] is True


# ---------------------------------------------------------------------------
# Interpreted replay tests
# ---------------------------------------------------------------------------


def test_send_replay_reconstructs_tag_values(monkeypatch: pytest.MonkeyPatch):
    futures = _fake_send_submit(monkeypatch)

    logic = _make_send_program()
    runner = PLC(logic=logic)

    runner.patch({"Enable": True, "Source": 42})
    runner.step()  # scan 1: submit
    assert runner.current_state.tags["Sending"] is True

    runner.step()  # scan 2: in-flight
    assert runner.current_state.tags["Sending"] is True

    futures[0].set_result(_sr._RequestResult(ok=True, exception_code=0))
    runner.patch({"Enable": True})
    runner.step()  # scan 3: drain
    assert runner.current_state.tags["Sending"] is False
    assert runner.current_state.tags["Success"] is True

    fork1 = runner.replay_to(1)
    assert fork1.current_state.tags["Sending"] is True
    assert fork1.current_state.tags["Success"] is False

    fork2 = runner.replay_to(2)
    assert fork2.current_state.tags["Sending"] is True

    fork3 = runner.replay_to(3)
    assert fork3.current_state.tags["Sending"] is False
    assert fork3.current_state.tags["Success"] is True
    assert fork3.current_state.tags["Error"] is False


def test_receive_replay_reconstructs_dest_tag(monkeypatch: pytest.MonkeyPatch):
    futures = _fake_recv_submit(monkeypatch)

    logic = _make_receive_program()
    runner = PLC(logic=logic)

    runner.patch({"Enable": True})
    runner.step()  # scan 1: submit
    assert runner.current_state.tags["Receiving"] is True

    futures[0].set_result(_sr._RequestResult(ok=True, exception_code=0, values=(777,)))
    runner.patch({"Enable": True})
    runner.step()  # scan 2: drain
    assert runner.current_state.tags["Dest"] == 777
    assert runner.current_state.tags["Success"] is True

    fork1 = runner.replay_to(1)
    assert fork1.current_state.tags["Receiving"] is True
    assert fork1.current_state.tags.get("Dest", 0) == 0

    fork2 = runner.replay_to(2)
    assert fork2.current_state.tags["Receiving"] is False
    assert fork2.current_state.tags["Dest"] == 777
    assert fork2.current_state.tags["Success"] is True


def test_send_error_replay(monkeypatch: pytest.MonkeyPatch):
    futures = _fake_send_submit(monkeypatch)

    logic = _make_send_program()
    runner = PLC(logic=logic)

    runner.patch({"Enable": True, "Source": 1})
    runner.step()  # scan 1: submit

    futures[0].set_result(_sr._RequestResult(ok=False, exception_code=3))
    runner.patch({"Enable": True})
    runner.step()  # scan 2: drain with error

    assert runner.current_state.tags["Error"] is True
    assert runner.current_state.tags["ExCode"] == 3

    fork = runner.replay_to(2)
    assert fork.current_state.tags["Sending"] is False
    assert fork.current_state.tags["Error"] is True
    assert fork.current_state.tags["ExCode"] == 3


def test_rung_conditioned_on_sending_evaluates_during_replay(monkeypatch: pytest.MonkeyPatch):
    """``with Rung(Sending): ...`` should see correct values during replay."""
    futures = _fake_send_submit(monkeypatch)

    Enable = Bool("Enable")
    Source = Int("Source")
    Sending = Bool("Sending")
    Success = Bool("Success")
    Error = Bool("Error")
    ExCode = Int("ExCode")
    Indicator = Bool("Indicator")

    with Program(strict=False) as logic:
        with Rung(Enable):
            _sr.send(
                target=_TARGET,
                remote_start="DS1",
                source=Source,
                sending=Sending,
                success=Success,
                error=Error,
                exception_response=ExCode,
            )
        with Rung(Sending):
            out(Indicator)

    runner = PLC(logic=logic)
    runner.patch({"Enable": True, "Source": 1})
    runner.step()  # scan 1: submit -> Sending=True -> Indicator=True
    assert runner.current_state.tags["Indicator"] is True

    futures[0].set_result(_sr._RequestResult(ok=True, exception_code=0))
    runner.patch({"Enable": False})
    runner.step()  # scan 2: drain -> Sending=False -> Indicator=False
    assert runner.current_state.tags["Indicator"] is False

    fork1 = runner.replay_to(1)
    assert fork1.current_state.tags["Sending"] is True
    assert fork1.current_state.tags["Indicator"] is True

    fork2 = runner.replay_to(2)
    assert fork2.current_state.tags["Sending"] is False
    assert fork2.current_state.tags["Indicator"] is False


def test_multi_scan_inflight(monkeypatch: pytest.MonkeyPatch):
    futures = _fake_send_submit(monkeypatch)

    logic = _make_send_program()
    runner = PLC(logic=logic)

    runner.patch({"Enable": True, "Source": 1})
    runner.step()  # scan 1: submit

    for _ in range(3):
        runner.step()  # scans 2-4: in-flight

    futures[0].set_result(_sr._RequestResult(ok=True, exception_code=0))
    runner.patch({"Enable": True})
    runner.step()  # scan 5: drain

    assert runner.current_state.scan_id == 5

    for sid in [1, 2, 3, 4]:
        fork = runner.replay_to(sid)
        assert fork.current_state.tags["Sending"] is True, f"scan {sid}"
        assert fork.current_state.tags["Success"] is False, f"scan {sid}"

    fork5 = runner.replay_to(5)
    assert fork5.current_state.tags["Sending"] is False
    assert fork5.current_state.tags["Success"] is True


# ---------------------------------------------------------------------------
# Compiled replay test
# ---------------------------------------------------------------------------


def test_compiled_replay_applies_io_tag_writes():
    """Compiled replay applies I/O tag_writes directly to kernel.tags."""
    from pyrung.circuitpy.codegen import compile_kernel

    Light = Bool("Light")
    with Program() as simple_logic:
        with Rung():
            out(Light)

    simple_runner = PLC(logic=simple_logic)
    simple_runner.step()  # scan 1
    simple_runner.step()  # scan 2

    submit_rec = IoSubmitRecord(
        tag_writes=(("Sending", True), ("Success", False), ("Error", False), ("ExCode", 0))
    )
    drain_rec = IoResultRecord(
        ok=True,
        exception_code=0,
        values=(),
        tag_writes=(("Sending", False), ("Success", True), ("Error", False), ("ExCode", 0)),
    )
    simple_runner._scan_log.record_io_submit(1, "send:test:Sending", submit_rec)
    simple_runner._scan_log.record_io_drain(2, "send:test:Sending", drain_rec)

    kernel = compile_kernel(simple_logic)
    fork = simple_runner._replay_to_compiled(2, kernel)

    assert fork.current_state.tags["Sending"] is False
    assert fork.current_state.tags["Success"] is True
    assert fork.current_state.tags["Error"] is False


# ---------------------------------------------------------------------------
# Fork inertness
# ---------------------------------------------------------------------------


def test_fork_does_not_receive_recorded_io(monkeypatch: pytest.MonkeyPatch):
    """Forks are inert -- no recorded I/O should be applied to a plain fork."""
    _fake_send_submit(monkeypatch)

    logic = _make_send_program()
    runner = PLC(logic=logic)

    runner.patch({"Enable": True, "Source": 1})
    runner.step()  # scan 1: submit
    assert runner.current_state.tags["Sending"] is True

    fork = runner.fork()
    fork.step()

    assert fork.current_state.tags["Sending"] is True


# ---------------------------------------------------------------------------
# scan_log maintenance
# ---------------------------------------------------------------------------


def test_trim_before_removes_io_records():
    from pyrung.core.scan_log import ScanLog
    from pyrung.core.time_mode import TimeMode

    log = ScanLog(time_mode=TimeMode.FIXED_STEP)
    log.record_io_submit(1, "k", IoSubmitRecord(tag_writes=(("A", True),)))
    log.record_io_drain(
        2, "k", IoResultRecord(ok=True, exception_code=0, values=(), tag_writes=(("A", False),))
    )
    log.record_io_submit(5, "k", IoSubmitRecord(tag_writes=(("A", True),)))

    log.trim_before(3)
    snap = log.snapshot()
    assert 1 not in snap.io_submits_by_scan
    assert 2 not in snap.io_drains_by_scan
    assert 5 in snap.io_submits_by_scan


def test_bytes_estimate_includes_io():
    from pyrung.core.scan_log import ScanLog
    from pyrung.core.time_mode import TimeMode

    log = ScanLog(time_mode=TimeMode.FIXED_STEP)
    empty_size = log.bytes_estimate()
    log.record_io_submit(1, "k", IoSubmitRecord(tag_writes=(("A", True),)))
    assert log.bytes_estimate() > empty_size
