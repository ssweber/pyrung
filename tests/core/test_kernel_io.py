"""Which send()/receive(), nop() and raw() programs get a runner kernel, and what it does."""

from __future__ import annotations

from concurrent.futures import Future

import pytest

from pyrung import Bool, Int, Program, Rung, Timer, copy, on_delay, out
from pyrung.circuitpy import P1AM, generate_circuitpy
from pyrung.click import ModbusTcpTarget, nop, raw, receive, send
from pyrung.core.instruction.send_receive import ModbusAddress, RegisterType
from pyrung.core.instruction.send_receive import _core as _sr
from pyrung.core.runner import PLC

SCANS = 2500
NEVER = Bool("KioNever")
Enable, Walk, Lamp = Bool("KioEnable"), Bool("KioWalk"), Bool("KioLamp")
Level, Local, Source = Int("KioLevel"), Int("KioLocal"), Int("KioSource")
RxBusy, RxOk, RxErr, RxCode = Bool("KioRxBusy"), Bool("KioRxOk"), Bool("KioRxErr"), Int("KioRxCode")
TxBusy, TxOk = Bool("KioTxBusy"), Bool("KioTxOk")
Wait = Timer.clone("KioWait")
_LIVE = ModbusTcpTarget("kio", "127.0.0.1", port=502, device_id=1)


def _receive_program(target):
    """A small examples/circuitpy_traffic_light_modbus.py, with a timer and a receive each scan."""
    with Program(strict=False) as program:
        with Rung(Enable):
            on_delay(Wait, 50)
        with Rung(Wait.Done):
            out(Lamp)
        with Rung():
            receive(
                target=target,
                remote_start=ModbusAddress(0, RegisterType.COIL) if target == "panel" else "C1",
                dest=Walk,
                receiving=RxBusy,
                success=RxOk,
                error=RxErr,
                exception_response=RxCode,
            )
        with Rung(Wait.Done):
            send(target=target, remote_start="DS1", source=Source, sending=TxBusy, success=TxOk)
    return program


def _nop_raw_program():
    with Program(strict=False) as program:
        with Rung():
            nop()
        with Rung(Enable):
            on_delay(Wait, 50)
            raw("Email", "0x2737,1,60a5=1")
        with Rung(Wait.Done):
            out(Lamp)
    return program


def _run(program, *, stepped: bool) -> PLC:
    plc = PLC(program, dt=0.01)
    plc.patch({Enable.name: True, Walk.name: True, RxCode.name: 7, RxOk.name: True})
    if stepped:
        for _ in range(SCANS):
            plc.run_until(NEVER, max_cycles=1, jit=False)
    else:
        plc.run_until(NEVER, max_cycles=SCANS)
    return plc


@pytest.mark.parametrize(
    "program",
    [_receive_program("panel"), _nop_raw_program()],
    ids=["string target", "nop and raw"],
)
def test_the_program_gets_a_kernel_and_runs_as_stepping_does(program) -> None:
    ran = _run(program, stepped=False)
    twin = _run(program, stepped=True)
    assert ran._compiled_replay_supported_kernel() is not None
    assert ran.state.scan_id == twin.state.scan_id == SCANS
    assert dict(ran.state.tags) == dict(twin.state.tags)
    assert dict(ran.state.memory) == dict(twin.state.memory)
    assert ran.state.tags[Lamp.name] is True
    ran._recent_state_cache.clear()
    ran._replay_slabs.clear()
    for scan in (1, 4, 6, SCANS - 300):
        assert ran.history.at(scan).tags == twin.history.at(scan).tags


def test_a_string_target_writes_nothing_stepped_or_replayed() -> None:
    plc = _run(_receive_program("panel"), stepped=True)
    tags = plc.state.tags
    # The patched values stay, and the other status bits keep their defaults.
    assert (tags[Walk.name], tags[RxCode.name], tags[RxOk.name]) == (True, 7, True)
    assert not any(tags[tag.name] for tag in (RxBusy, RxErr, TxBusy, TxOk))
    snapshot = plc._scan_log.snapshot()
    assert not snapshot.io_submits_by_scan and not snapshot.io_drains_by_scan
    replayed = plc.replay_to(SCANS - 1)
    assert replayed.state.tags == plc.history.at(SCANS - 1).tags


def test_a_live_target_still_gets_no_kernel() -> None:
    """A kernel would write recorded I/O after its scan (stepping writes it at the instruction)."""
    assert PLC(_receive_program(_LIVE), dt=0.01)._compiled_replay_supported_kernel() is None


@pytest.mark.parametrize("instruction", [nop, lambda: raw("Email", "0x1")], ids=["nop", "raw"])
def test_a_code_py_still_refuses_nop_and_raw(instruction) -> None:
    hardware = P1AM()
    hardware.slot(1, "P1-08SIM")
    with Program(strict=False) as program:
        with Rung():
            instruction()
    with pytest.raises(NotImplementedError, match="Unsupported instruction type"):
        generate_circuitpy(program, hardware, target_scan_ms=10.0)


def _answered_at_once(monkeypatch: pytest.MonkeyPatch) -> list[dict]:
    calls: list[dict] = []

    def submit(**kwargs):
        calls.append(kwargs)
        future: Future[_sr._RequestResult] = Future()
        future.set_result(_sr._RequestResult(ok=True, exception_code=0, values=(1,)))
        return future

    monkeypatch.setattr(_sr, "_submit_click_send_request", submit)
    monkeypatch.setattr(_sr, "_submit_click_receive_request", submit)
    return calls


def _live_send_program():
    with Program(strict=False) as program:
        with Rung(Enable):
            send(target=_LIVE, remote_start="DS1", source=Source, sending=TxBusy, success=TxOk)
    return program


@pytest.mark.xfail(
    strict=True,
    reason="fork() shares the Program, so a fork collects the parent's request (runner.py fork)",
)
def test_a_fork_does_not_collect_its_parent_s_request(monkeypatch: pytest.MonkeyPatch) -> None:
    _answered_at_once(monkeypatch)
    parent = PLC(_live_send_program(), dt=0.01)
    parent.patch({Enable.name: True})
    parent.step()  # submits, and the answer is already in
    fork = parent.fork()
    fork.step()
    parent.step()
    assert parent.state.tags[TxOk.name] is True
    assert fork.state.tags.get(TxOk.name) is not True


@pytest.mark.xfail(
    strict=True,
    reason="compiled replay writes recorded I/O after the scan, stepping writes it mid-scan",
)
def test_compiled_replay_of_live_io_matches_stepping(monkeypatch: pytest.MonkeyPatch) -> None:
    """No run meets this today, since a live program gets no kernel.  It pins what one would do."""
    from pyrung.circuitpy.codegen import compile_kernel

    _answered_at_once(monkeypatch)
    with Program(strict=False) as program:
        with Rung(Enable):
            send(target=_LIVE, remote_start="DS1", source=Source, sending=TxBusy, success=TxOk)
        with Rung(TxBusy):
            out(Lamp)
    plc = PLC(program, dt=0.01)
    plc.patch({Enable.name: True})
    plc.step()
    assert plc.state.tags[Lamp.name] is True
    replayed = plc._replay_to_compiled(1, compile_kernel(program))
    assert replayed.state.tags[Lamp.name] is True


def test_how_runs_no_modbus_and_steers_what_the_device_supplies(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = _answered_at_once(monkeypatch)
    with Program(strict=False) as program:
        with Rung():
            receive(target=_LIVE, remote_start="DS1", dest=Level, receiving=RxBusy, success=RxOk)
        with Rung(RxOk):
            copy(Level, Local)
        with Rung(Local > 5):
            out(Lamp)
    plc = PLC(program, dt=0.01)
    plan = plc.how(Lamp, max_scans=2000, wall_clock_s=60)
    assert plan.reachable
    assert calls == []
    assert plan.fork._compiled_replay_supported_kernel() is not None
    assert plan.changes[RxOk.name] is True
    assert plan.changes[Level.name] > 5
    text = str(plan)
    assert text.count("how() does not simulate Modbus send()/receive().") == 1
    assert text.count("This value stands for the Modbus device's answer.") == 2
    assert text.isascii()

    plc.run_for(0.05)
    assert calls
    assert plc._compiled_replay_supported_kernel() is None
