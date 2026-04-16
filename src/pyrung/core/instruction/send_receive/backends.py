"""Automatically generated module split."""

from __future__ import annotations

import asyncio
import re
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass
from typing import Any

from pyclickplc import ClickClient
from pyclickplc.addresses import format_address_display
from pyclickplc.banks import BANKS

from pyrung.core.tag import Tag

from .helpers import _contiguous_runs
from .types import ModbusRtuTarget, ModbusTcpTarget, RegisterType

_EXECUTOR = ThreadPoolExecutor(max_workers=4, thread_name_prefix="pyrung-modbus")
_DEFAULT_TIMEOUT_SECONDS = 1

# ---------------------------------------------------------------------------
# Async Modbus backend — Click path
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _RequestResult:
    ok: bool
    exception_code: int
    values: tuple[Any, ...] = ()


@dataclass
class _PendingRequest:
    future: Future[_RequestResult]
    target_tags: list[Tag] | None = None


def _submit_click_send_request(
    *,
    host: str,
    port: int,
    device_id: int,
    bank: str,
    addresses: tuple[int, ...],
    values: tuple[Any, ...],
) -> Future[_RequestResult]:
    return _EXECUTOR.submit(
        _run_click_send_request,
        host,
        port,
        device_id,
        bank,
        addresses,
        values,
    )


def _submit_click_receive_request(
    *,
    host: str,
    port: int,
    device_id: int,
    bank: str,
    start: int,
    end: int,
) -> Future[_RequestResult]:
    return _EXECUTOR.submit(
        _run_click_receive_request,
        host,
        port,
        device_id,
        bank,
        start,
        end,
    )


def _run_click_send_request(
    host: str,
    port: int,
    device_id: int,
    bank: str,
    addresses: tuple[int, ...],
    values: tuple[Any, ...],
) -> _RequestResult:
    if not addresses:
        return _RequestResult(ok=False, exception_code=0)
    if len(addresses) != len(values):
        return _RequestResult(ok=False, exception_code=0)

    async def _run() -> _RequestResult:
        try:
            async with ClickClient(
                host,
                port,
                timeout=_DEFAULT_TIMEOUT_SECONDS,
                device_id=device_id,
            ) as plc:
                cfg = BANKS[bank]
                if cfg.valid_ranges is None:
                    start_addr = format_address_display(bank, addresses[0])
                    payload: Any = values[0] if len(values) == 1 else list(values)
                    await plc.addr.write(start_addr, payload)
                else:
                    for run_start_addr, run_lo, run_hi in _contiguous_runs(addresses):
                        run_values = values[run_lo:run_hi]
                        start_addr = format_address_display(bank, run_start_addr)
                        payload = run_values[0] if len(run_values) == 1 else list(run_values)
                        await plc.addr.write(start_addr, payload)
            return _RequestResult(ok=True, exception_code=0)
        except Exception as exc:
            return _RequestResult(ok=False, exception_code=_extract_exception_code(exc))

    try:
        return asyncio.run(_run())
    except Exception as exc:
        return _RequestResult(ok=False, exception_code=_extract_exception_code(exc))


def _run_click_receive_request(
    host: str,
    port: int,
    device_id: int,
    bank: str,
    start: int,
    end: int,
) -> _RequestResult:
    async def _run() -> _RequestResult:
        try:
            async with ClickClient(
                host,
                port,
                timeout=_DEFAULT_TIMEOUT_SECONDS,
                device_id=device_id,
            ) as plc:
                start_addr = format_address_display(bank, start)
                if start == end:
                    response = await plc.addr.read(start_addr)
                else:
                    end_addr = format_address_display(bank, end)
                    response = await plc.addr.read(f"{start_addr}-{end_addr}")
                return _RequestResult(ok=True, exception_code=0, values=tuple(response.values()))
        except Exception as exc:
            return _RequestResult(ok=False, exception_code=_extract_exception_code(exc))

    try:
        return asyncio.run(_run())
    except Exception as exc:
        return _RequestResult(ok=False, exception_code=_extract_exception_code(exc))


# ---------------------------------------------------------------------------
# Async Modbus backend — raw path (pymodbus)
# ---------------------------------------------------------------------------


def _create_raw_client(
    target: ModbusTcpTarget | ModbusRtuTarget,
) -> Any:
    from pymodbus.client import ModbusSerialClient, ModbusTcpClient

    if isinstance(target, ModbusTcpTarget):
        return ModbusTcpClient(target.ip, port=target.port, timeout=target.timeout_ms / 1000.0)
    return ModbusSerialClient(
        port=target.serial_port,
        baudrate=target.baudrate,
        bytesize=target.bytesize,
        parity=target.parity,
        stopbits=target.stopbits,
        timeout=target.timeout_ms / 1000.0,
    )


def _submit_raw_send_request(
    *,
    target: ModbusTcpTarget | ModbusRtuTarget,
    address: int,
    register_type: RegisterType,
    registers: list[Any],
    device_id: int,
) -> Future[_RequestResult]:
    return _EXECUTOR.submit(
        _run_raw_send_request,
        target,
        address,
        register_type,
        registers,
        device_id,
    )


def _run_raw_send_request(
    target: ModbusTcpTarget | ModbusRtuTarget,
    address: int,
    register_type: RegisterType,
    registers: list[Any],
    device_id: int,
) -> _RequestResult:
    if not registers:
        return _RequestResult(ok=False, exception_code=0)
    client = _create_raw_client(target)
    try:
        client.connect()
        if register_type == RegisterType.HOLDING:
            if len(registers) == 1:
                response = client.write_register(address, registers[0], device_id=device_id)
            else:
                response = client.write_registers(address, registers, device_id=device_id)
        elif register_type == RegisterType.COIL:
            if len(registers) == 1:
                response = client.write_coil(address, registers[0], device_id=device_id)
            else:
                response = client.write_coils(address, registers, device_id=device_id)
        else:
            return _RequestResult(ok=False, exception_code=0)
        if response.isError():
            code = getattr(response, "exception_code", 0) or 0
            return _RequestResult(ok=False, exception_code=int(code))
        return _RequestResult(ok=True, exception_code=0)
    except Exception as exc:
        return _RequestResult(ok=False, exception_code=_extract_exception_code(exc))
    finally:
        client.close()


def _submit_raw_receive_request(
    *,
    target: ModbusTcpTarget | ModbusRtuTarget,
    address: int,
    register_type: RegisterType,
    count: int,
    device_id: int,
) -> Future[_RequestResult]:
    return _EXECUTOR.submit(
        _run_raw_receive_request,
        target,
        address,
        register_type,
        count,
        device_id,
    )


def _run_raw_receive_request(
    target: ModbusTcpTarget | ModbusRtuTarget,
    address: int,
    register_type: RegisterType,
    count: int,
    device_id: int,
) -> _RequestResult:
    client = _create_raw_client(target)
    try:
        client.connect()
        if register_type == RegisterType.HOLDING:
            response = client.read_holding_registers(address, count=count, device_id=device_id)
        elif register_type == RegisterType.INPUT:
            response = client.read_input_registers(address, count=count, device_id=device_id)
        elif register_type == RegisterType.COIL:
            response = client.read_coils(address, count=count, device_id=device_id)
        elif register_type == RegisterType.DISCRETE_INPUT:
            response = client.read_discrete_inputs(address, count=count, device_id=device_id)
        else:
            return _RequestResult(ok=False, exception_code=0)
        if response.isError():
            code = getattr(response, "exception_code", 0) or 0
            return _RequestResult(ok=False, exception_code=int(code))
        if register_type in {RegisterType.COIL, RegisterType.DISCRETE_INPUT}:
            return _RequestResult(ok=True, exception_code=0, values=tuple(response.bits[:count]))
        return _RequestResult(ok=True, exception_code=0, values=tuple(response.registers))
    except Exception as exc:
        return _RequestResult(ok=False, exception_code=_extract_exception_code(exc))
    finally:
        client.close()


# ---------------------------------------------------------------------------
# Shared backend helpers
# ---------------------------------------------------------------------------


def _extract_exception_code(exc: BaseException) -> int:
    """Best-effort extraction of Modbus exception codes from wrapped client errors."""

    match = re.search(r"exception_code=(\d+)", str(exc))
    if match is not None:
        return int(match.group(1))

    cause = exc.__cause__
    if cause is not None:
        nested_match = re.search(r"exception_code=(\d+)", str(cause))
        if nested_match is not None:
            return int(nested_match.group(1))
    return 0
