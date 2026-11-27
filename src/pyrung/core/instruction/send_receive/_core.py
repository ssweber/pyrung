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

import itertools
from concurrent.futures import Future
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, assert_never

from pyclickplc.addresses import parse_address

from pyrung.core._source import _capture_source
from pyrung.core.memory_block import BlockRange
from pyrung.core.program.context import _require_rung_context
from pyrung.core.scan_log import IoResultRecord, IoSubmitRecord
from pyrung.core.tag import Tag, TagType

from ..base import Instruction
from ..conversions import _store_copy_value_to_tag_type
from ..operands import PayloadOperand, RangeArg, TagArg, TextInto, check_rules
from . import backends as _backends
from .helpers import (
    _addresses_for_count,
    _normalize_operand_count,
    _normalize_operand_tags,
    _pack_values_to_registers,
    _preview_operand_tag_types,
    _register_count,
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
# Status bits
# ---------------------------------------------------------------------------

#: Name prefix of the hidden tag a status bit gets when it is left off.
MODBUS_STATUS_TAG_PREFIX = "_modbus."

_status_site_counter = itertools.count(1)


def is_modbus_status_tag(name: str) -> bool:
    """Return whether *name* is the hidden tag of a send/receive status bit that was left off."""
    return name.startswith(MODBUS_STATUS_TAG_PREFIX)


def _status_tags(
    busy_name: str,
    busy: Tag | None,
    success: Tag | None,
    error: Tag | None,
    exception_response: Tag | None,
) -> tuple[Tag, Tag, Tag, Tag]:
    """Return the four status tags, with a hidden tag for each bit left off (CLICK allows any)."""
    site = next(_status_site_counter)

    def resolve(tag: Tag | None, field_name: str, tag_type: TagType) -> Tag:
        if tag is not None:
            return tag
        return Tag(
            f"{MODBUS_STATUS_TAG_PREFIX}{site}.{field_name}",
            tag_type,
            comment=f"{field_name} left off",
        )

    return (
        resolve(busy, busy_name, TagType.BOOL),
        resolve(success, "success", TagType.BOOL),
        resolve(error, "error", TagType.BOOL),
        resolve(exception_response, "exception_response", TagType.INT),
    )


def _device_id_value(device_id: int | Tag, ctx: ScanContext) -> int:
    """Return the device id: the number itself, or the value its DS tag holds as the request starts.

    A value that no device can answer to is passed on as it is.  What a CLICK does with
    such a value is not yet known.
    """
    if isinstance(device_id, Tag):
        return int(ctx.get_tag(device_id.name, device_id.default))
    return device_id


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

    _reads = ("source", "device_id")
    _writes = ("sending", "success", "error", "exception_response")
    _conditions = ()
    _structural_fields = ("target_name", "bank", "start", "addresses")

    target_name: str
    bank: str | None
    start: int
    addresses: tuple[int, ...]
    source: TagArg | RangeArg
    sending: Tag
    success: Tag
    error: Tag
    exception_response: Tag
    host: str | None = None
    port: int = 502
    device_id: int | Tag = 1
    timeout_s: float = 1.0
    remote_address: ModbusAddress | None = None
    word_swap: bool = False
    register_count: int = 0
    raw_target: ModbusTcpTarget | ModbusRtuTarget | None = None

    def always_execute(self) -> bool:
        return True

    def is_inert_when_disabled(self) -> bool:
        return False

    def _is_live(self) -> bool:
        return self.host is not None or self.raw_target is not None

    def _submit(
        self, source_tags: list[Tag], values: list[Any], device_id: int
    ) -> Future[_RequestResult]:
        if self.bank is not None:
            assert self.host is not None
            return _submit_click_send_request(
                host=self.host,
                port=self.port,
                device_id=device_id,
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
            device_id=device_id,
        )

    @property
    def _io_key(self) -> str:
        return f"send:{self.target_name}:{self.sending.name}"

    def execute(self, ctx: ScanContext, enabled: bool) -> None:
        io = ctx.modbus_io
        if io == "replay":
            self._execute_replay(ctx, enabled)
        elif io == "live" and self._is_live():
            self._execute_live(ctx, enabled)

    def _execute_live(self, ctx: ScanContext, enabled: bool) -> None:
        key = self._io_key

        pending = ctx.modbus_requests.get(id(self))
        if pending is not None:
            if not pending.future.done():
                ctx.set_tag(self.sending.name, True)
                return

            try:
                result = pending.future.result()
            except Exception:
                result = _RequestResult(ok=False, exception_code=0)

            del ctx.modbus_requests[id(self)]
            if result.ok:
                drain_writes: dict[str, Any] = {
                    self.sending.name: False,
                    self.success.name: True,
                    self.error.name: False,
                    self.exception_response.name: 0,
                }
            else:
                drain_writes = {
                    self.sending.name: False,
                    self.success.name: False,
                    self.error.name: True,
                    self.exception_response.name: int(result.exception_code),
                }
            ctx.set_tags(drain_writes)
            ctx.record_io_drain(
                key,
                IoResultRecord(
                    ok=result.ok,
                    exception_code=result.exception_code,
                    values=(),
                    tag_writes=tuple(drain_writes.items()),
                ),
            )
            return

        if not enabled:
            return

        source_tags = _normalize_operand_tags(self.source, ctx)
        values = [ctx.get_tag(tag.name, tag.default) for tag in source_tags]
        device_id = _device_id_value(self.device_id, ctx)
        ctx.modbus_requests[id(self)] = _PendingRequest(
            future=self._submit(source_tags, values, device_id)
        )
        submit_writes: dict[str, Any] = {
            self.sending.name: True,
            self.success.name: False,
            self.error.name: False,
            self.exception_response.name: 0,
        }
        ctx.set_tags(submit_writes)
        ctx.record_io_submit(key, IoSubmitRecord(tag_writes=tuple(submit_writes.items())))

    def _execute_replay(self, ctx: ScanContext, enabled: bool) -> None:
        key = self._io_key
        drain = ctx.get_replay_io_drain(key)
        if drain is not None:
            ctx.set_tags(dict(drain.tag_writes))
            return
        if ctx.get_tag(self.sending.name) is True:
            return
        if not enabled:
            return
        if ctx.has_replay_io_submit(key):
            ctx.set_tags(
                {
                    self.sending.name: True,
                    self.success.name: False,
                    self.error.name: False,
                    self.exception_response.name: 0,
                }
            )


@dataclass
class ModbusReceiveInstruction(Instruction):
    """Modbus receive (read from remote device).

    Live I/O is performed when a connection target is set (``host`` for
    Click TCP, ``raw_target`` for raw Modbus).  When neither is set the
    instruction is a simulation-inert placeholder for code generation.
    """

    _reads = ("device_id",)
    _writes = ("dest", "receiving", "success", "error", "exception_response")
    _conditions = ()
    _structural_fields = ("target_name", "bank", "start", "addresses")

    target_name: str
    bank: str | None
    start: int
    addresses: tuple[int, ...]
    dest: TagArg | RangeArg
    receiving: Tag
    success: Tag
    error: Tag
    exception_response: Tag
    host: str | None = None
    port: int = 502
    device_id: int | Tag = 1
    timeout_s: float = 1.0
    remote_address: ModbusAddress | None = None
    word_swap: bool = False
    register_count: int = 0
    raw_target: ModbusTcpTarget | ModbusRtuTarget | None = None

    def always_execute(self) -> bool:
        return True

    def is_inert_when_disabled(self) -> bool:
        return False

    @property
    def external_payload_names(self) -> frozenset[str]:
        """Destination tags whose values come from the injected I/O result."""
        match self.dest:
            case TagArg(tag=tag):
                return frozenset({tag.name})
            case RangeArg(span=span):
                return frozenset(tag.name for tag in span.tags())
            case _:
                assert_never(self.dest)

    def _is_live(self) -> bool:
        return self.host is not None or self.raw_target is not None

    def _submit(self, device_id: int) -> Future[_RequestResult]:
        if self.bank is not None:
            assert self.host is not None
            return _submit_click_receive_request(
                host=self.host,
                port=self.port,
                device_id=device_id,
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
            device_id=device_id,
        )

    def _unpack_result(self, result: _RequestResult, target_tags: list[Tag]) -> tuple[Any, ...]:
        if self.bank is not None:
            return result.values
        assert self.remote_address is not None
        word_order = WordOrder.LOW_HIGH if self.word_swap else WordOrder.HIGH_LOW
        return _unpack_registers_to_values(
            list(result.values), target_tags, word_order, self.remote_address.register_type
        )

    @property
    def _io_key(self) -> str:
        return f"recv:{self.target_name}:{self.receiving.name}"

    def execute(self, ctx: ScanContext, enabled: bool) -> None:
        io = ctx.modbus_io
        if io == "replay":
            self._execute_replay(ctx, enabled)
        elif io == "live" and self._is_live():
            self._execute_live(ctx, enabled)

    def _execute_live(self, ctx: ScanContext, enabled: bool) -> None:
        key = self._io_key

        pending = ctx.modbus_requests.get(id(self))
        if pending is not None:
            if not pending.future.done():
                ctx.set_tag(self.receiving.name, True)
                return

            target_tags = pending.target_tags or []
            try:
                result = pending.future.result()
            except Exception:
                result = _RequestResult(ok=False, exception_code=0)

            del ctx.modbus_requests[id(self)]
            if not result.ok:
                drain_writes: dict[str, Any] = {
                    self.receiving.name: False,
                    self.success.name: False,
                    self.error.name: True,
                    self.exception_response.name: int(result.exception_code),
                }
                ctx.set_tags(drain_writes)
                ctx.record_io_drain(
                    key,
                    IoResultRecord(
                        ok=False,
                        exception_code=result.exception_code,
                        values=(),
                        tag_writes=tuple(drain_writes.items()),
                    ),
                )
                return

            unpacked = self._unpack_result(result, target_tags)
            if len(unpacked) != len(target_tags):
                drain_writes = {
                    self.receiving.name: False,
                    self.success.name: False,
                    self.error.name: True,
                    self.exception_response.name: 0,
                }
                ctx.set_tags(drain_writes)
                ctx.record_io_drain(
                    key,
                    IoResultRecord(
                        ok=False,
                        exception_code=0,
                        values=(),
                        tag_writes=tuple(drain_writes.items()),
                    ),
                )
                return

            typed_values = tuple(
                _store_copy_value_to_tag_type(value, tag)
                for tag, value in zip(target_tags, unpacked, strict=True)
            )
            drain_writes = {
                tag.name: typed_val
                for tag, typed_val in zip(target_tags, typed_values, strict=True)
            }
            drain_writes[self.receiving.name] = False
            drain_writes[self.success.name] = True
            drain_writes[self.error.name] = False
            drain_writes[self.exception_response.name] = 0
            ctx.set_tags(drain_writes)
            ctx.record_io_drain(
                key,
                IoResultRecord(
                    ok=True,
                    exception_code=0,
                    values=typed_values,
                    tag_writes=tuple(drain_writes.items()),
                ),
            )
            return

        if not enabled:
            return

        dest_tags = _normalize_operand_tags(self.dest, ctx)
        ctx.modbus_requests[id(self)] = _PendingRequest(
            future=self._submit(_device_id_value(self.device_id, ctx)),
            target_tags=dest_tags,
        )
        submit_writes: dict[str, Any] = {
            self.receiving.name: True,
            self.success.name: False,
            self.error.name: False,
            self.exception_response.name: 0,
        }
        ctx.set_tags(submit_writes)
        ctx.record_io_submit(key, IoSubmitRecord(tag_writes=tuple(submit_writes.items())))

    def _execute_replay(self, ctx: ScanContext, enabled: bool) -> None:
        key = self._io_key
        drain = ctx.get_replay_io_drain(key)
        if drain is not None:
            ctx.set_tags(dict(drain.tag_writes))
            return
        if ctx.get_tag(self.receiving.name) is True:
            return
        if not enabled:
            return
        if ctx.has_replay_io_submit(key):
            ctx.set_tags(
                {
                    self.receiving.name: True,
                    self.success.name: False,
                    self.error.name: False,
                    self.exception_response.name: 0,
                }
            )


# ---------------------------------------------------------------------------
# Public DSL functions
# ---------------------------------------------------------------------------


_PAYLOAD = PayloadOperand()
_RECEIVE_TEXT = TextInto(value="remote_start", dest="dest", writes="receive")
_SEND_TEXT = TextInto(value="remote_start", dest="source", writes="send")


def _resolve_target(
    target: str | ModbusTcpTarget | ModbusRtuTarget,
) -> tuple[str, str | None, int, int | Tag, float, ModbusTcpTarget | ModbusRtuTarget | None]:
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
            raise TypeError(
                "target is an empty string. "
                "Pass a target name, a ModbusTcpTarget or a ModbusRtuTarget."
            )
        return (target, None, 502, 1, 1.0, None)
    raise TypeError(
        f"target must be str, ModbusTcpTarget, or ModbusRtuTarget, got {type(target).__name__}"
    )


def _resolve_remote_start(
    remote_start: str | ModbusAddress,
    operand: TagArg | RangeArg,
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
            raise ValueError(
                f"send() cannot write to {remote_start.register_type.name} addresses, "
                "because they are read-only. Use a HOLDING or COIL address."
            )
        tag_types = _preview_operand_tag_types(operand, effective_count)
        register_count = _register_count(
            tag_types, remote_start.register_type, "send" if is_send else "receive"
        )
        start_addr = remote_start.address
        addresses = tuple(range(start_addr, start_addr + register_count))
        return (None, start_addr, addresses, remote_start, register_count)

    raise TypeError(f"remote_start must be str or ModbusAddress, got {type(remote_start).__name__}")


def send(
    *,
    target: str | ModbusTcpTarget | ModbusRtuTarget,
    remote_start: str | ModbusAddress,
    source: Tag | BlockRange,
    sending: Tag | None = None,
    success: Tag | None = None,
    error: Tag | None = None,
    exception_response: Tag | None = None,
    count: int | None = None,
    word_swap: bool = False,
) -> None:
    """Modbus send instruction (write local values to a remote device).

    ``target`` may be a :class:`ModbusTcpTarget` or :class:`ModbusRtuTarget`
    (live simulation) or a plain string name (codegen placeholder).

    ``remote_start`` may be a Click address string (e.g. ``"DS1"``) for Click
    PLCs, or a :class:`ModbusAddress` for raw Modbus devices.

    Status bits are optional, as in CLICK.  If you leave a status bit off, the instruction
    still writes that bit, to a hidden tag.  CLICK export leaves the hidden tag off.
    """
    sending, success, error, exception_response = _status_tags(
        "sending", sending, success, error, exception_response
    )
    _validate_status_tags(
        busy=sending,
        success=success,
        error=error,
        exception_response=exception_response,
        busy_name="sending",
    )
    payload = _PAYLOAD.parse("send()", "source", source)

    target_name, host, port, device_id, timeout_s, raw_target = _resolve_target(target)
    bank, start_addr, addresses, remote_address, register_count = _resolve_remote_start(
        remote_start, payload, count, target, is_send=True
    )
    check_rules({}, (_SEND_TEXT,), "send()", {"remote_start": remote_start, "source": source})

    ctx = _require_rung_context("send")
    source_file, source_line = _capture_source(depth=2)
    instr = ModbusSendInstruction(
        target_name=target_name,
        bank=bank,
        start=start_addr,
        addresses=addresses,
        source=payload,
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
    receiving: Tag | None = None,
    success: Tag | None = None,
    error: Tag | None = None,
    exception_response: Tag | None = None,
    count: int | None = None,
    word_swap: bool = False,
) -> None:
    """Modbus receive instruction (read remote device values into local tags).

    ``target`` may be a :class:`ModbusTcpTarget` or :class:`ModbusRtuTarget`
    (live simulation) or a plain string name (codegen placeholder).

    ``remote_start`` may be a Click address string (e.g. ``"DS1"``) for Click
    PLCs, or a :class:`ModbusAddress` for raw Modbus devices.

    Status bits are optional, as in CLICK.  If you leave a status bit off, the instruction
    still writes that bit, to a hidden tag.  CLICK export leaves the hidden tag off.
    """
    receiving, success, error, exception_response = _status_tags(
        "receiving", receiving, success, error, exception_response
    )
    _validate_status_tags(
        busy=receiving,
        success=success,
        error=error,
        exception_response=exception_response,
        busy_name="receiving",
    )
    payload = _PAYLOAD.parse("receive()", "dest", dest)

    target_name, host, port, device_id, timeout_s, raw_target = _resolve_target(target)
    if isinstance(raw_target, ModbusRtuTarget) and isinstance(device_id, int) and device_id == 0:
        raise ValueError(
            "receive() cannot read from device_id 0 on an RTU target. 0 is the broadcast "
            "address, and no device answers it. Use a device_id from 1 to 247, or a DS tag."
        )
    bank, start_addr, addresses, remote_address, register_count = _resolve_remote_start(
        remote_start, payload, count, target, is_send=False
    )
    check_rules({}, (_RECEIVE_TEXT,), "receive()", {"remote_start": remote_start, "dest": dest})

    ctx = _require_rung_context("receive")
    source_file, source_line = _capture_source(depth=2)
    instr = ModbusReceiveInstruction(
        target_name=target_name,
        bank=bank,
        start=start_addr,
        addresses=addresses,
        dest=payload,
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
    "MODBUS_STATUS_TAG_PREFIX",
    "ModbusAddress",
    "ModbusReceiveInstruction",
    "ModbusRtuTarget",
    "ModbusSendInstruction",
    "ModbusTcpTarget",
    "RegisterType",
    "VALID_COM_PORTS",
    "WordOrder",
    "is_modbus_status_tag",
    "receive",
    "send",
]
