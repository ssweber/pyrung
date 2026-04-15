"""Modbus send/receive DSL instructions.

These instructions support two backend paths:

- **Click path** — ``remote_start`` is a Click address string (e.g. ``"DS1"``).
  Uses ``pyclickplc.ClickClient`` for live I/O.
- **Raw path** — ``remote_start`` is a :class:`ModbusAddress`.  Uses
  ``pymodbus`` directly for live I/O over TCP or RTU.

When constructed with a plain target-name string the instruction is inert
during simulation and exists only for code generation.
"""

from __future__ import annotations

from concurrent.futures import Future
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from pyclickplc.addresses import parse_address

from pyrung.core._source import _capture_source
from pyrung.core.memory_block import BlockRange
from pyrung.core.program.context import _require_rung_context
from pyrung.core.tag import Tag

from ..base import Instruction
from ..conversions import _store_copy_value_to_tag_type
from . import backends as _backends
from .helpers import (
    _addresses_for_count,
    _calculate_register_count,
    _normalize_operand_count,
    _normalize_operand_tags,
    _pack_values_to_registers,
    _preview_operand_tag_types,
    _status_clear_tags,
    _unpack_registers_to_values,
    _validate_status_tags,
)
from .types import (
    VALID_COM_PORTS,
    ModbusAddress,
    ModbusRtuTarget,
    ModbusTcpTarget,
    RegisterType,
    WordOrder,
)

if TYPE_CHECKING:
    from pyrung.core.context import ScanContext

ClickClient = _backends.ClickClient
_PendingRequest = _backends._PendingRequest
_RequestResult = _backends._RequestResult
_create_raw_client = _backends._create_raw_client
_discard_pending_request = _backends._discard_pending_request
_extract_exception_code = _backends._extract_exception_code
_run_raw_receive_request = _backends._run_raw_receive_request
_run_raw_send_request = _backends._run_raw_send_request


def _submit_click_send_request(
    *,
    host: str,
    port: int,
    device_id: int,
    bank: str,
    addresses: tuple[int, ...],
    values: tuple[Any, ...],
) -> Future[_RequestResult]:
    _backends.ClickClient = ClickClient
    return _backends._submit_click_send_request(
        host=host,
        port=port,
        device_id=device_id,
        bank=bank,
        addresses=addresses,
        values=values,
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
    _backends.ClickClient = ClickClient
    return _backends._submit_click_receive_request(
        host=host,
        port=port,
        device_id=device_id,
        bank=bank,
        start=start,
        end=end,
    )


def _run_click_send_request(
    host: str,
    port: int,
    device_id: int,
    bank: str,
    addresses: tuple[int, ...],
    values: tuple[Any, ...],
) -> _RequestResult:
    _backends.ClickClient = ClickClient
    return _backends._run_click_send_request(
        host,
        port,
        device_id,
        bank,
        addresses,
        values,
    )


def _run_click_receive_request(
    host: str,
    port: int,
    device_id: int,
    bank: str,
    start: int,
    end: int,
) -> _RequestResult:
    _backends.ClickClient = ClickClient
    return _backends._run_click_receive_request(
        host,
        port,
        device_id,
        bank,
        start,
        end,
    )


def _submit_raw_send_request(
    *,
    target: ModbusTcpTarget | ModbusRtuTarget,
    address: int,
    register_type: RegisterType,
    registers: list[Any],
    device_id: int,
) -> Future[_RequestResult]:
    return _backends._submit_raw_send_request(
        target=target,
        address=address,
        register_type=register_type,
        registers=registers,
        device_id=device_id,
    )


def _submit_raw_receive_request(
    *,
    target: ModbusTcpTarget | ModbusRtuTarget,
    address: int,
    register_type: RegisterType,
    count: int,
    device_id: int,
) -> Future[_RequestResult]:
    return _backends._submit_raw_receive_request(
        target=target,
        address=address,
        register_type=register_type,
        count=count,
        device_id=device_id,
    )


# ---------------------------------------------------------------------------
# Instruction classes
# ---------------------------------------------------------------------------


@dataclass
class ModbusSendInstruction(Instruction):
    """Modbus send (write to remote device).

    Live I/O is performed when a connection target is set (``host`` for
    Click TCP, ``raw_target`` for raw Modbus).  When neither is set the
    instruction is a simulation-inert placeholder for code generation.
    """

    _reads = ("source",)
    _writes = ("sending", "success", "error", "exception_response")
    _conditions = ()
    _structural_fields = ("target_name", "bank", "start", "addresses")

    target_name: str
    bank: str | None
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
    remote_address: ModbusAddress | None = None
    word_swap: bool = False
    register_count: int = 0
    raw_target: ModbusTcpTarget | ModbusRtuTarget | None = None
    _pending: _PendingRequest | None = field(default=None, init=False, repr=False)

    def always_execute(self) -> bool:
        return True

    def is_inert_when_disabled(self) -> bool:
        return False

    def _is_live(self) -> bool:
        return self.host is not None or self.raw_target is not None

    def _submit(self, source_tags: list[Tag], values: list[Any]) -> Future[_RequestResult]:
        if self.bank is not None:
            return _submit_click_send_request(
                host=self.host,  # ty: ignore[invalid-argument-type]
                port=self.port,
                device_id=self.device_id,
                bank=self.bank,
                addresses=self.addresses,
                values=tuple(values),
            )
        assert self.remote_address is not None
        assert self.raw_target is not None
        word_order = WordOrder.LOW_HIGH if self.word_swap else WordOrder.HIGH_LOW
        registers = _pack_values_to_registers(
            values, source_tags, word_order, self.remote_address.register_type
        )
        return _submit_raw_send_request(
            target=self.raw_target,
            address=self.remote_address.address,
            register_type=self.remote_address.register_type,
            registers=registers,
            device_id=self.device_id,
        )

    def execute(self, ctx: ScanContext, enabled: bool) -> None:
        if not enabled:
            if self._is_live():
                _discard_pending_request(self._pending)
                self._pending = None
            ctx.set_tags(
                _status_clear_tags(self.sending, self.success, self.error, self.exception_response)
            )
            return

        if not self._is_live():
            return

        if self._pending is None:
            source_tags = _normalize_operand_tags(self.source, ctx)
            values = [ctx.get_tag(tag.name, tag.default) for tag in source_tags]
            self._pending = _PendingRequest(future=self._submit(source_tags, values))
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
    """Modbus receive (read from remote device).

    Live I/O is performed when a connection target is set (``host`` for
    Click TCP, ``raw_target`` for raw Modbus).  When neither is set the
    instruction is a simulation-inert placeholder for code generation.
    """

    _reads = ()
    _writes = ("dest", "receiving", "success", "error", "exception_response")
    _conditions = ()
    _structural_fields = ("target_name", "bank", "start", "addresses")

    target_name: str
    bank: str | None
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
    remote_address: ModbusAddress | None = None
    word_swap: bool = False
    register_count: int = 0
    raw_target: ModbusTcpTarget | ModbusRtuTarget | None = None
    _pending: _PendingRequest | None = field(default=None, init=False, repr=False)

    def always_execute(self) -> bool:
        return True

    def is_inert_when_disabled(self) -> bool:
        return False

    def _is_live(self) -> bool:
        return self.host is not None or self.raw_target is not None

    def _submit(self, dest_tags: list[Tag]) -> Future[_RequestResult]:
        if self.bank is not None:
            return _submit_click_receive_request(
                host=self.host,  # ty: ignore[invalid-argument-type]
                port=self.port,
                device_id=self.device_id,
                bank=self.bank,
                start=self.start,
                end=self.addresses[-1],
            )
        assert self.remote_address is not None
        assert self.raw_target is not None
        return _submit_raw_receive_request(
            target=self.raw_target,
            address=self.remote_address.address,
            register_type=self.remote_address.register_type,
            count=self.register_count,
            device_id=self.device_id,
        )

    def _unpack_result(self, result: _RequestResult, target_tags: list[Tag]) -> tuple[Any, ...]:
        if self.bank is not None:
            return result.values
        assert self.remote_address is not None
        word_order = WordOrder.LOW_HIGH if self.word_swap else WordOrder.HIGH_LOW
        return _unpack_registers_to_values(
            list(result.values), target_tags, word_order, self.remote_address.register_type
        )

    def execute(self, ctx: ScanContext, enabled: bool) -> None:
        if not enabled:
            if self._is_live():
                _discard_pending_request(self._pending)
                self._pending = None
            ctx.set_tags(
                _status_clear_tags(
                    self.receiving, self.success, self.error, self.exception_response
                )
            )
            return

        if not self._is_live():
            return

        if self._pending is None:
            dest_tags = _normalize_operand_tags(self.dest, ctx)
            self._pending = _PendingRequest(
                future=self._submit(dest_tags),
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

        unpacked = self._unpack_result(result, target_tags)
        if len(unpacked) != len(target_tags):
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
            for tag, value in zip(target_tags, unpacked, strict=True)
        }
        updates[self.receiving.name] = False
        updates[self.success.name] = True
        updates[self.error.name] = False
        updates[self.exception_response.name] = 0
        ctx.set_tags(updates)


# ---------------------------------------------------------------------------
# Public DSL functions
# ---------------------------------------------------------------------------


def _resolve_target(
    target: str | ModbusTcpTarget | ModbusRtuTarget,
) -> tuple[str, str | None, int, int, float, ModbusTcpTarget | ModbusRtuTarget | None]:
    """Extract connection params from a target value.

    Returns ``(target_name, host, port, device_id, timeout_s, raw_target)``.
    """
    if isinstance(target, ModbusTcpTarget):
        return (
            target.name,
            target.ip,
            target.port,
            target.device_id,
            target.timeout_ms / 1000.0,
            target,
        )
    if isinstance(target, ModbusRtuTarget):
        return (
            target.name,
            None,
            502,
            target.device_id,
            target.timeout_ms / 1000.0,
            target,
        )
    if isinstance(target, str):
        if not target:
            raise TypeError("target must be a non-empty string or ModbusTcpTarget/ModbusRtuTarget")
        return (target, None, 502, 1, 1.0, None)
    raise TypeError(
        f"target must be str, ModbusTcpTarget, or ModbusRtuTarget, got {type(target).__name__}"
    )


def _resolve_remote_start(
    remote_start: str | ModbusAddress,
    operand: Tag | BlockRange,
    count: int | None,
    target: str | ModbusTcpTarget | ModbusRtuTarget,
    *,
    is_send: bool,
) -> tuple[str | None, int, tuple[int, ...], ModbusAddress | None, int]:
    """Resolve remote address for Click or raw Modbus.

    Returns ``(bank, start_addr, addresses, remote_address, register_count)``.
    """
    effective_count = _normalize_operand_count(operand, count)

    if isinstance(remote_start, str):
        bank, start_addr = parse_address(remote_start)
        addresses = _addresses_for_count(bank, start_addr, effective_count)
        return (bank, start_addr, addresses, None, 0)

    if isinstance(remote_start, ModbusAddress):
        if is_send and remote_start.register_type in {
            RegisterType.INPUT,
            RegisterType.DISCRETE_INPUT,
        }:
            raise ValueError(f"Cannot send (write) to {remote_start.register_type.name} registers")
        tag_types = _preview_operand_tag_types(operand, effective_count)
        register_count = _calculate_register_count(tag_types, remote_start.register_type)
        start_addr = remote_start.address
        addresses = tuple(range(start_addr, start_addr + register_count))
        return (None, start_addr, addresses, remote_start, register_count)

    raise TypeError(f"remote_start must be str or ModbusAddress, got {type(remote_start).__name__}")


def send(
    *,
    target: str | ModbusTcpTarget | ModbusRtuTarget,
    remote_start: str | ModbusAddress,
    source: Tag | BlockRange,
    sending: Tag,
    success: Tag,
    error: Tag,
    exception_response: Tag,
    count: int | None = None,
    word_swap: bool = False,
) -> None:
    """Modbus send instruction (write local values to a remote device).

    ``target`` may be a :class:`ModbusTcpTarget` or :class:`ModbusRtuTarget`
    (live simulation) or a plain string name (codegen placeholder).

    ``remote_start`` may be a Click address string (e.g. ``"DS1"``) for Click
    PLCs, or a :class:`ModbusAddress` for raw Modbus devices.
    """
    _validate_status_tags(
        busy=sending,
        success=success,
        error=error,
        exception_response=exception_response,
        busy_name="sending",
    )
    if not isinstance(source, (Tag, BlockRange)):
        raise TypeError(f"source must be Tag or BlockRange, got {type(source).__name__}")

    target_name, host, port, device_id, timeout_s, raw_target = _resolve_target(target)
    bank, start_addr, addresses, remote_address, register_count = _resolve_remote_start(
        remote_start, source, count, target, is_send=True
    )

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
        remote_address=remote_address,
        word_swap=word_swap,
        register_count=register_count,
        raw_target=raw_target,
    )
    instr.source_file, instr.source_line = source_file, source_line
    ctx._rung.add_instruction(instr)


def receive(
    *,
    target: str | ModbusTcpTarget | ModbusRtuTarget,
    remote_start: str | ModbusAddress,
    dest: Tag | BlockRange,
    receiving: Tag,
    success: Tag,
    error: Tag,
    exception_response: Tag,
    count: int | None = None,
    word_swap: bool = False,
) -> None:
    """Modbus receive instruction (read remote device values into local tags).

    ``target`` may be a :class:`ModbusTcpTarget` or :class:`ModbusRtuTarget`
    (live simulation) or a plain string name (codegen placeholder).

    ``remote_start`` may be a Click address string (e.g. ``"DS1"``) for Click
    PLCs, or a :class:`ModbusAddress` for raw Modbus devices.
    """
    _validate_status_tags(
        busy=receiving,
        success=success,
        error=error,
        exception_response=exception_response,
        busy_name="receiving",
    )
    if not isinstance(dest, (Tag, BlockRange)):
        raise TypeError(f"dest must be Tag or BlockRange, got {type(dest).__name__}")

    target_name, host, port, device_id, timeout_s, raw_target = _resolve_target(target)
    bank, start_addr, addresses, remote_address, register_count = _resolve_remote_start(
        remote_start, dest, count, target, is_send=False
    )

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
        remote_address=remote_address,
        word_swap=word_swap,
        register_count=register_count,
        raw_target=raw_target,
    )
    instr.source_file, instr.source_line = source_file, source_line
    ctx._rung.add_instruction(instr)


__all__ = [
    "ModbusAddress",
    "ModbusReceiveInstruction",
    "ModbusRtuTarget",
    "ModbusSendInstruction",
    "ModbusTcpTarget",
    "RegisterType",
    "VALID_COM_PORTS",
    "WordOrder",
    "receive",
    "send",
]
