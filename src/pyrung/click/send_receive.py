"""Modbus TCP send/receive DSL instructions.

These instructions are Click-address aware.  When constructed with a
``ModbusTarget`` (which carries host/port/device_id), the instruction
performs live asynchronous Modbus I/O during simulation.  When constructed
with a plain target-name string, the instruction is inert during simulation
and exists only for code generation (CircuitPython).
"""

from __future__ import annotations

import asyncio
import re
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from pyclickplc import ClickClient
from pyclickplc.addresses import format_address_display, parse_address
from pyclickplc.banks import BANKS

from pyrung.core._source import _capture_source
from pyrung.core.instruction import (
    Instruction,
    resolve_block_range_tags_ctx,
    resolve_tag_ctx,
)
from pyrung.core.instruction.conversions import _store_copy_value_to_tag_type
from pyrung.core.memory_block import BlockRange
from pyrung.core.program.context import _require_rung_context
from pyrung.core.tag import Tag, TagType

if TYPE_CHECKING:
    from pyrung.core.context import ScanContext

_EXECUTOR = ThreadPoolExecutor(max_workers=4, thread_name_prefix="pyrung-click-modbus")
_DEFAULT_TIMEOUT_SECONDS = 1


# ---------------------------------------------------------------------------
# ModbusTarget — connection details for a remote PLC
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ModbusTarget:
    """Connection details for a remote Modbus TCP device."""

    name: str
    ip: str
    port: int = 502
    device_id: int = 1
    timeout_ms: int = 1000

    def __post_init__(self) -> None:
        if not isinstance(self.name, str):
            raise TypeError(f"name must be str, got {type(self.name).__name__}")
        if not self.name:
            raise ValueError("name must not be empty")
        if not isinstance(self.ip, str):
            raise TypeError(f"ip must be str, got {type(self.ip).__name__}")
        if not self.ip:
            raise ValueError("ip must not be empty")
        if not isinstance(self.port, int):
            raise TypeError(f"port must be int, got {type(self.port).__name__}")
        if self.port < 1 or self.port > 65535:
            raise ValueError("port must be in 1..65535")
        if not isinstance(self.device_id, int):
            raise TypeError(f"device_id must be int, got {type(self.device_id).__name__}")
        if self.device_id < 0 or self.device_id > 255:
            raise ValueError("device_id must be in 0..255")
        if not isinstance(self.timeout_ms, int):
            raise TypeError(f"timeout_ms must be int, got {type(self.timeout_ms).__name__}")
        if self.timeout_ms <= 0:
            raise ValueError("timeout_ms must be > 0")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _is_valid_index(bank: str, index: int) -> bool:
    cfg = BANKS[bank]
    if cfg.valid_ranges is None:
        return cfg.min_addr <= index <= cfg.max_addr
    return any(lo <= index <= hi for lo, hi in cfg.valid_ranges)


def _range_end_for_count(bank: str, start: int, count: int) -> int:
    return _addresses_for_count(bank, start, count)[-1]


def _addresses_for_count(bank: str, start: int, count: int) -> tuple[int, ...]:
    if count <= 0:
        raise ValueError("count must be >= 1")

    if not _is_valid_index(bank, start):
        raise ValueError(f"{bank} address {start} is out of range")

    cfg = BANKS[bank]
    if cfg.valid_ranges is None:
        end = start + count - 1
        if end > cfg.max_addr:
            raise ValueError(
                f"{bank} range overflow: start {start}, count {count} exceeds {cfg.max_addr}"
            )
        return tuple(range(start, end + 1))

    addresses = [start]
    current = start
    while len(addresses) < count:
        current += 1
        while current <= cfg.max_addr and not _is_valid_index(bank, current):
            current += 1
        if current > cfg.max_addr:
            raise ValueError(
                f"{bank} range overflow: start {start}, count {count} exceeds valid addresses"
            )
        addresses.append(current)
    return tuple(addresses)


def _contiguous_runs(addresses: tuple[int, ...]) -> list[tuple[int, int, int]]:
    runs: list[tuple[int, int, int]] = []
    run_start_addr = addresses[0]
    run_start_idx = 0
    prev_addr = addresses[0]

    for idx, addr in enumerate(addresses[1:], start=1):
        if addr != prev_addr + 1:
            runs.append((run_start_addr, run_start_idx, idx))
            run_start_addr = addr
            run_start_idx = idx
        prev_addr = addr

    runs.append((run_start_addr, run_start_idx, len(addresses)))
    return runs


def _normalize_operand_tags(operand: Tag | BlockRange, ctx: ScanContext) -> list[Tag]:
    if isinstance(operand, Tag):
        return [resolve_tag_ctx(operand, ctx)]
    return resolve_block_range_tags_ctx(operand, ctx)


def _normalize_operand_count(operand: Tag | BlockRange, count: int | None) -> int:
    expected = 1 if isinstance(operand, Tag) else len(tuple(operand.addresses))
    effective = expected if count is None else count
    if effective != expected:
        raise ValueError(
            f"count mismatch: operand resolves to {expected} tag(s) but count={effective}"
        )
    return expected


def _status_clear_tags(
    busy: Tag, success: Tag, error: Tag, exception_response: Tag
) -> dict[str, Any]:
    return {
        busy.name: False,
        success.name: False,
        error.name: False,
        exception_response.name: 0,
    }


def _validate_status_tags(
    *,
    busy: Tag,
    success: Tag,
    error: Tag,
    exception_response: Tag,
    busy_name: str,
) -> None:
    if busy.type != TagType.BOOL:
        raise TypeError(f"{busy_name} tag '{busy.name}' must be BOOL")
    if success.type != TagType.BOOL:
        raise TypeError(f"success tag '{success.name}' must be BOOL")
    if error.type != TagType.BOOL:
        raise TypeError(f"error tag '{error.name}' must be BOOL")
    if exception_response.type not in {TagType.INT, TagType.DINT}:
        raise TypeError(f"exception_response tag '{exception_response.name}' must be INT or DINT")


# ---------------------------------------------------------------------------
# Async Modbus backend (used only for live simulation)
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


def _submit_send_request(
    *,
    host: str,
    port: int,
    device_id: int,
    bank: str,
    addresses: tuple[int, ...],
    values: tuple[Any, ...],
) -> Future[_RequestResult]:
    return _EXECUTOR.submit(
        _run_send_request,
        host,
        port,
        device_id,
        bank,
        addresses,
        values,
    )


def _submit_receive_request(
    *,
    host: str,
    port: int,
    device_id: int,
    bank: str,
    start: int,
    end: int,
) -> Future[_RequestResult]:
    return _EXECUTOR.submit(
        _run_receive_request,
        host,
        port,
        device_id,
        bank,
        start,
        end,
    )


def _run_send_request(
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


def _run_receive_request(
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


def _discard_pending_request(pending: _PendingRequest | None) -> None:
    if pending is None:
        return
    # Best effort only: a running threadpool task may not cancel.
    pending.future.cancel()


# ---------------------------------------------------------------------------
# Instruction classes
# ---------------------------------------------------------------------------


@dataclass
class ModbusSendInstruction(Instruction):
    """Modbus TCP send (write to remote PLC).

    When ``host`` is set, ``execute()`` performs live asynchronous Modbus I/O.
    When ``host`` is None, ``execute()`` is a simulation-inert placeholder
    for code generation.
    """

    target_name: str
    bank: str
    start: int
    addresses: tuple[int, ...]
    source: Tag | BlockRange
    sending: Tag
    success: Tag
    error: Tag
    exception_response: Tag
    host: str | None = None
    port: int = 502
    device_id: int = 1
    timeout_s: float = 1.0
    _pending: _PendingRequest | None = field(default=None, init=False, repr=False)

    def always_execute(self) -> bool:
        return True

    def is_inert_when_disabled(self) -> bool:
        return False

    def execute(self, ctx: ScanContext, enabled: bool) -> None:
        if not enabled:
            if self.host is not None:
                _discard_pending_request(self._pending)
                self._pending = None
            ctx.set_tags(
                _status_clear_tags(self.sending, self.success, self.error, self.exception_response)
            )
            return

        # Inert mode — codegen placeholder
        if self.host is None:
            return

        # Live Modbus I/O
        if self._pending is None:
            source_tags = _normalize_operand_tags(self.source, ctx)
            values = [ctx.get_tag(tag.name, tag.default) for tag in source_tags]
            self._pending = _PendingRequest(
                future=_submit_send_request(
                    host=self.host,
                    port=self.port,
                    device_id=self.device_id,
                    bank=self.bank,
                    addresses=self.addresses,
                    values=tuple(values),
                )
            )
            ctx.set_tags(
                {
                    self.sending.name: True,
                    self.success.name: False,
                    self.error.name: False,
                    self.exception_response.name: 0,
                }
            )
            return

        ctx.set_tag(self.sending.name, True)
        if not self._pending.future.done():
            return

        try:
            result = self._pending.future.result()
        except Exception:
            result = _RequestResult(ok=False, exception_code=0)

        self._pending = None
        if result.ok:
            ctx.set_tags(
                {
                    self.sending.name: False,
                    self.success.name: True,
                    self.error.name: False,
                    self.exception_response.name: 0,
                }
            )
        else:
            ctx.set_tags(
                {
                    self.sending.name: False,
                    self.success.name: False,
                    self.error.name: True,
                    self.exception_response.name: int(result.exception_code),
                }
            )


@dataclass
class ModbusReceiveInstruction(Instruction):
    """Modbus TCP receive (read from remote PLC).

    When ``host`` is set, ``execute()`` performs live asynchronous Modbus I/O.
    When ``host`` is None, ``execute()`` is a simulation-inert placeholder
    for code generation.
    """

    target_name: str
    bank: str
    start: int
    addresses: tuple[int, ...]
    dest: Tag | BlockRange
    receiving: Tag
    success: Tag
    error: Tag
    exception_response: Tag
    host: str | None = None
    port: int = 502
    device_id: int = 1
    timeout_s: float = 1.0
    _pending: _PendingRequest | None = field(default=None, init=False, repr=False)

    def always_execute(self) -> bool:
        return True

    def is_inert_when_disabled(self) -> bool:
        return False

    def execute(self, ctx: ScanContext, enabled: bool) -> None:
        if not enabled:
            if self.host is not None:
                _discard_pending_request(self._pending)
                self._pending = None
            ctx.set_tags(
                _status_clear_tags(
                    self.receiving, self.success, self.error, self.exception_response
                )
            )
            return

        # Inert mode — codegen placeholder
        if self.host is None:
            return

        # Live Modbus I/O
        if self._pending is None:
            dest_tags = _normalize_operand_tags(self.dest, ctx)
            end = self.addresses[-1]

            self._pending = _PendingRequest(
                future=_submit_receive_request(
                    host=self.host,
                    port=self.port,
                    device_id=self.device_id,
                    bank=self.bank,
                    start=self.start,
                    end=end,
                ),
                target_tags=dest_tags,
            )
            ctx.set_tags(
                {
                    self.receiving.name: True,
                    self.success.name: False,
                    self.error.name: False,
                    self.exception_response.name: 0,
                }
            )
            return

        ctx.set_tag(self.receiving.name, True)
        if not self._pending.future.done():
            return

        target_tags = self._pending.target_tags or []
        try:
            result = self._pending.future.result()
        except Exception:
            result = _RequestResult(ok=False, exception_code=0)

        self._pending = None
        if not result.ok:
            ctx.set_tags(
                {
                    self.receiving.name: False,
                    self.success.name: False,
                    self.error.name: True,
                    self.exception_response.name: int(result.exception_code),
                }
            )
            return

        if len(result.values) != len(target_tags):
            ctx.set_tags(
                {
                    self.receiving.name: False,
                    self.success.name: False,
                    self.error.name: True,
                    self.exception_response.name: 0,
                }
            )
            return

        updates = {
            tag.name: _store_copy_value_to_tag_type(value, tag)
            for tag, value in zip(target_tags, result.values, strict=True)
        }
        updates[self.receiving.name] = False
        updates[self.success.name] = True
        updates[self.error.name] = False
        updates[self.exception_response.name] = 0
        ctx.set_tags(updates)


# ---------------------------------------------------------------------------
# Public DSL functions
# ---------------------------------------------------------------------------


def send(
    *,
    target: str | ModbusTarget,
    remote_start: str,
    source: Tag | BlockRange,
    sending: Tag,
    success: Tag,
    error: Tag,
    exception_response: Tag,
    count: int | None = None,
) -> None:
    """Modbus TCP send instruction (write local values to a remote PLC).

    ``target`` may be a :class:`ModbusTarget` (live simulation) or a plain
    string name (codegen placeholder, resolved at code-generation time via
    ``ModbusClientConfig``).
    """
    _validate_status_tags(
        busy=sending,
        success=success,
        error=error,
        exception_response=exception_response,
        busy_name="sending",
    )
    if isinstance(target, ModbusTarget):
        target_name = target.name
        host: str | None = target.ip
        port = target.port
        device_id = target.device_id
        timeout_s = target.timeout_ms / 1000.0
    elif isinstance(target, str):
        if not target:
            raise TypeError("target must be a non-empty string or ModbusTarget")
        target_name = target
        host = None
        port = 502
        device_id = 1
        timeout_s = 1.0
    else:
        raise TypeError(f"target must be str or ModbusTarget, got {type(target).__name__}")

    if not isinstance(source, (Tag, BlockRange)):
        raise TypeError(f"source must be Tag or BlockRange, got {type(source).__name__}")

    bank, start_addr = parse_address(remote_start)
    effective_count = _normalize_operand_count(source, count)
    addresses = _addresses_for_count(bank, start_addr, effective_count)

    ctx = _require_rung_context("send")
    source_file, source_line = _capture_source(depth=2)
    instr = ModbusSendInstruction(
        target_name=target_name,
        bank=bank,
        start=start_addr,
        addresses=addresses,
        source=source,
        sending=sending,
        success=success,
        error=error,
        exception_response=exception_response,
        host=host,
        port=port,
        device_id=device_id,
        timeout_s=timeout_s,
    )
    instr.source_file, instr.source_line = source_file, source_line
    ctx._rung.add_instruction(instr)


def receive(
    *,
    target: str | ModbusTarget,
    remote_start: str,
    dest: Tag | BlockRange,
    receiving: Tag,
    success: Tag,
    error: Tag,
    exception_response: Tag,
    count: int | None = None,
) -> None:
    """Modbus TCP receive instruction (read remote PLC values into local tags).

    ``target`` may be a :class:`ModbusTarget` (live simulation) or a plain
    string name (codegen placeholder, resolved at code-generation time via
    ``ModbusClientConfig``).
    """
    _validate_status_tags(
        busy=receiving,
        success=success,
        error=error,
        exception_response=exception_response,
        busy_name="receiving",
    )
    if isinstance(target, ModbusTarget):
        target_name = target.name
        host: str | None = target.ip
        port = target.port
        device_id = target.device_id
        timeout_s = target.timeout_ms / 1000.0
    elif isinstance(target, str):
        if not target:
            raise TypeError("target must be a non-empty string or ModbusTarget")
        target_name = target
        host = None
        port = 502
        device_id = 1
        timeout_s = 1.0
    else:
        raise TypeError(f"target must be str or ModbusTarget, got {type(target).__name__}")

    if not isinstance(dest, (Tag, BlockRange)):
        raise TypeError(f"dest must be Tag or BlockRange, got {type(dest).__name__}")

    bank, start_addr = parse_address(remote_start)
    effective_count = _normalize_operand_count(dest, count)
    addresses = _addresses_for_count(bank, start_addr, effective_count)

    ctx = _require_rung_context("receive")
    source_file, source_line = _capture_source(depth=2)
    instr = ModbusReceiveInstruction(
        target_name=target_name,
        bank=bank,
        start=start_addr,
        addresses=addresses,
        dest=dest,
        receiving=receiving,
        success=success,
        error=error,
        exception_response=exception_response,
        host=host,
        port=port,
        device_id=device_id,
        timeout_s=timeout_s,
    )
    instr.source_file, instr.source_line = source_file, source_line
    ctx._rung.add_instruction(instr)


__all__ = ["ModbusReceiveInstruction", "ModbusSendInstruction", "ModbusTarget", "receive", "send"]
