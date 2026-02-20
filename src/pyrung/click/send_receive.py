"""CLICK Modbus send/receive DSL instructions.

These instructions are CLICK-address aware and execute asynchronous Modbus TCP
requests in a background worker so scan execution remains synchronous.
"""

from __future__ import annotations

import asyncio
import re
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from pyclickplc import ClickClient
from pyclickplc.addresses import format_address_display, parse_address
from pyclickplc.banks import BANKS

from pyrung.core.instruction import (
    Instruction,
    resolve_block_range_tags_ctx,
    resolve_tag_ctx,
)
from pyrung.core.instruction.conversions import _store_copy_value_to_tag_type
from pyrung.core.memory_block import BlockRange
from pyrung.core.program import _require_rung_context
from pyrung.core.tag import Tag, TagType

if TYPE_CHECKING:
    from pyrung.core.condition import Condition
    from pyrung.core.context import ScanContext

_EXECUTOR = ThreadPoolExecutor(max_workers=4, thread_name_prefix="pyrung-click-modbus")
_DEFAULT_TIMEOUT_SECONDS = 1


def _is_valid_index(bank: str, index: int) -> bool:
    cfg = BANKS[bank]
    if cfg.valid_ranges is None:
        return cfg.min_addr <= index <= cfg.max_addr
    return any(lo <= index <= hi for lo, hi in cfg.valid_ranges)


def _range_end_for_count(bank: str, start: int, count: int) -> int:
    return _addresses_for_count(bank, start, count)[-1]


def _addresses_for_count(bank: str, start: int, count: int) -> list[int]:
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
        return list(range(start, end + 1))

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
    return addresses


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
    addresses: list[int],
    values: list[Any],
) -> Future[_RequestResult]:
    return _EXECUTOR.submit(
        _run_send_request,
        host,
        port,
        device_id,
        bank,
        tuple(addresses),
        tuple(values),
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


class _ClickSendInstruction(Instruction):
    def __init__(
        self,
        *,
        host: str,
        port: int,
        remote_start: str,
        source: Tag | BlockRange,
        sending: Tag,
        success: Tag,
        error: Tag,
        exception_response: Tag,
        device_id: int = 1,
        count: int | None = None,
        enable_condition: Condition | None = None,
    ) -> None:
        _validate_status_tags(
            busy=sending,
            success=success,
            error=error,
            exception_response=exception_response,
            busy_name="sending",
        )

        bank, start = parse_address(remote_start)
        self._bank = bank
        self._start = start
        self._host = host
        self._port = port
        self._source = source
        self._sending = sending
        self._success = success
        self._error = error
        self._exception_response = exception_response
        self._device_id = device_id
        self._count = count
        self._enable_condition = enable_condition
        self._pending: _PendingRequest | None = None

    def always_execute(self) -> bool:
        return True

    def _clear_status(self, ctx: ScanContext) -> None:
        ctx.set_tags(
            _status_clear_tags(self._sending, self._success, self._error, self._exception_response)
        )

    def execute(self, ctx: ScanContext, enabled: bool) -> None:
        if not enabled:
            _discard_pending_request(self._pending)
            self._pending = None
            self._clear_status(ctx)
            return

        if self._pending is None:
            source_tags = _normalize_operand_tags(self._source, ctx)
            expected_count = len(source_tags)
            count = expected_count if self._count is None else self._count
            if count != expected_count:
                raise ValueError(
                    f"send() count mismatch: source has {expected_count} tags but count={count}"
                )

            addresses = _addresses_for_count(self._bank, self._start, count)

            values = [ctx.get_tag(tag.name, tag.default) for tag in source_tags]
            self._pending = _PendingRequest(
                future=_submit_send_request(
                    host=self._host,
                    port=self._port,
                    device_id=self._device_id,
                    bank=self._bank,
                    addresses=addresses,
                    values=values,
                )
            )
            ctx.set_tags(
                {
                    self._sending.name: True,
                    self._success.name: False,
                    self._error.name: False,
                    self._exception_response.name: 0,
                }
            )
            return

        ctx.set_tag(self._sending.name, True)
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
                    self._sending.name: False,
                    self._success.name: True,
                    self._error.name: False,
                    self._exception_response.name: 0,
                }
            )
        else:
            ctx.set_tags(
                {
                    self._sending.name: False,
                    self._success.name: False,
                    self._error.name: True,
                    self._exception_response.name: int(result.exception_code),
                }
            )

    def is_inert_when_disabled(self) -> bool:
        return False


class _ClickReceiveInstruction(Instruction):
    def __init__(
        self,
        *,
        host: str,
        port: int,
        remote_start: str,
        dest: Tag | BlockRange,
        receiving: Tag,
        success: Tag,
        error: Tag,
        exception_response: Tag,
        device_id: int = 1,
        count: int | None = None,
        enable_condition: Condition | None = None,
    ) -> None:
        _validate_status_tags(
            busy=receiving,
            success=success,
            error=error,
            exception_response=exception_response,
            busy_name="receiving",
        )

        bank, start = parse_address(remote_start)
        self._bank = bank
        self._start = start
        self._host = host
        self._port = port
        self._dest = dest
        self._receiving = receiving
        self._success = success
        self._error = error
        self._exception_response = exception_response
        self._device_id = device_id
        self._count = count
        self._enable_condition = enable_condition
        self._pending: _PendingRequest | None = None

    def always_execute(self) -> bool:
        return True

    def _clear_status(self, ctx: ScanContext) -> None:
        ctx.set_tags(
            _status_clear_tags(
                self._receiving, self._success, self._error, self._exception_response
            )
        )

    def execute(self, ctx: ScanContext, enabled: bool) -> None:
        if not enabled:
            _discard_pending_request(self._pending)
            self._pending = None
            self._clear_status(ctx)
            return

        if self._pending is None:
            dest_tags = _normalize_operand_tags(self._dest, ctx)
            expected_count = len(dest_tags)
            count = expected_count if self._count is None else self._count
            if count != expected_count:
                raise ValueError(
                    f"receive() count mismatch: destination has {expected_count} tags but count={count}"
                )

            end = _range_end_for_count(self._bank, self._start, count)

            self._pending = _PendingRequest(
                future=_submit_receive_request(
                    host=self._host,
                    port=self._port,
                    device_id=self._device_id,
                    bank=self._bank,
                    start=self._start,
                    end=end,
                ),
                target_tags=dest_tags,
            )
            ctx.set_tags(
                {
                    self._receiving.name: True,
                    self._success.name: False,
                    self._error.name: False,
                    self._exception_response.name: 0,
                }
            )
            return

        ctx.set_tag(self._receiving.name, True)
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
                    self._receiving.name: False,
                    self._success.name: False,
                    self._error.name: True,
                    self._exception_response.name: int(result.exception_code),
                }
            )
            return

        if len(result.values) != len(target_tags):
            ctx.set_tags(
                {
                    self._receiving.name: False,
                    self._success.name: False,
                    self._error.name: True,
                    self._exception_response.name: 0,
                }
            )
            return

        updates = {
            tag.name: _store_copy_value_to_tag_type(value, tag)
            for tag, value in zip(target_tags, result.values, strict=True)
        }
        updates[self._receiving.name] = False
        updates[self._success.name] = True
        updates[self._error.name] = False
        updates[self._exception_response.name] = 0
        ctx.set_tags(updates)

    def is_inert_when_disabled(self) -> bool:
        return False


def send(
    *,
    host: str,
    port: int,
    remote_start: str,
    source: Tag | BlockRange,
    sending: Tag,
    success: Tag,
    error: Tag,
    exception_response: Tag,
    device_id: int = 1,
    count: int | None = None,
) -> None:
    """CLICK send instruction (Modbus TCP client write)."""
    ctx = _require_rung_context("send")
    enable_condition = ctx._rung._get_combined_condition()
    ctx._rung.add_instruction(
        _ClickSendInstruction(
            host=host,
            port=port,
            remote_start=remote_start,
            source=source,
            sending=sending,
            success=success,
            error=error,
            exception_response=exception_response,
            device_id=device_id,
            count=count,
            enable_condition=enable_condition,
        )
    )


def receive(
    *,
    host: str,
    port: int,
    remote_start: str,
    dest: Tag | BlockRange,
    receiving: Tag,
    success: Tag,
    error: Tag,
    exception_response: Tag,
    device_id: int = 1,
    count: int | None = None,
) -> None:
    """CLICK receive instruction (Modbus TCP client read)."""
    ctx = _require_rung_context("receive")
    enable_condition = ctx._rung._get_combined_condition()
    ctx._rung.add_instruction(
        _ClickReceiveInstruction(
            host=host,
            port=port,
            remote_start=remote_start,
            dest=dest,
            receiving=receiving,
            success=success,
            error=error,
            exception_response=exception_response,
            device_id=device_id,
            count=count,
            enable_condition=enable_condition,
        )
    )


__all__ = ["send", "receive"]
