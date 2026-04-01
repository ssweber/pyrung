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

import asyncio
import enum
import re
import struct
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from pyclickplc import ClickClient
from pyclickplc.addresses import format_address_display, parse_address
from pyclickplc.banks import BANKS

from pyrung.core._source import _capture_source
from pyrung.core.memory_block import BlockRange
from pyrung.core.program.context import _require_rung_context
from pyrung.core.tag import Tag, TagType

from .base import Instruction
from .conversions import _store_copy_value_to_tag_type
from .resolvers import resolve_block_range_tags_ctx, resolve_tag_ctx

if TYPE_CHECKING:
    from pyrung.core.context import ScanContext

_EXECUTOR = ThreadPoolExecutor(max_workers=4, thread_name_prefix="pyrung-modbus")
_DEFAULT_TIMEOUT_SECONDS = 1


# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------


class WordOrder(enum.Enum):
    """Word ordering for 32-bit values across register pairs."""

    HIGH_LOW = "high_low"
    LOW_HIGH = "low_high"


class RegisterType(enum.Enum):
    """Modbus register / coil type."""

    HOLDING = "holding"
    INPUT = "input"
    COIL = "coil"
    DISCRETE_INPUT = "discrete_input"


# ---------------------------------------------------------------------------
# ModbusAddress — Modbus register address
# ---------------------------------------------------------------------------

# MODBUS 984 address ranges (prefix encodes register type).
_984_RANGES: tuple[tuple[int, int, RegisterType], ...] = (
    (400001, 465536, RegisterType.HOLDING),
    (300001, 365536, RegisterType.INPUT),
    (100001, 165536, RegisterType.DISCRETE_INPUT),
)


def _infer_984(address: int) -> tuple[int, RegisterType] | None:
    """If *address* falls in a MODBUS 984 range, return (raw, register_type)."""
    for lo, hi, rt in _984_RANGES:
        if lo <= address < hi:
            return address - lo, rt
    return None


@dataclass(frozen=True)
class ModbusAddress:
    """Modbus register address.

    ``address`` accepts:

    * **MODBUS 984** ``int`` — e.g. ``400001`` (prefix encodes register type)
    * **MODBUS Hex** ``str`` with ``h`` suffix — e.g. ``"0h"``, ``"FFFEh"``
    * **Raw** ``int`` 0–0xFFFE — low-level register offset
    """

    address: int
    register_type: RegisterType = RegisterType.HOLDING

    def __post_init__(self) -> None:
        addr = self.address
        # --- hex string with "h" suffix (e.g. "0h", "FFFEh") ---
        if isinstance(addr, str):
            raw = addr.rstrip("hH")
            try:
                addr = int(raw, 16)
            except ValueError:
                raise ValueError(
                    f"address string must be valid hex with 'h' suffix, got {self.address!r}"
                ) from None
            object.__setattr__(self, "address", addr)
        if not isinstance(addr, int):
            raise TypeError(f"address must be int or hex str, got {type(self.address).__name__}")
        # --- MODBUS 984 range ---
        result = _infer_984(addr)
        if result is not None:
            raw_addr, inferred_rt = result
            object.__setattr__(self, "address", raw_addr)
            if self.register_type != RegisterType.HOLDING:
                # Caller explicitly set register_type — must match
                if self.register_type != inferred_rt:
                    raise ValueError(
                        f"984 address {addr} implies {inferred_rt.name}, "
                        f"but register_type={self.register_type.name}"
                    )
            else:
                object.__setattr__(self, "register_type", inferred_rt)
            addr = raw_addr
        # --- range check on resolved raw address ---
        if addr < 0 or addr > 0xFFFE:
            raise ValueError(f"address must be in 0..0xFFFE, got {addr}")
        if not isinstance(self.register_type, RegisterType):
            raise TypeError(
                f"register_type must be RegisterType, got {type(self.register_type).__name__}"
            )


# ---------------------------------------------------------------------------
# Target dataclasses
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ModbusTcpTarget:
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


VALID_COM_PORTS = frozenset({"cpu2", "slot0_1", "slot0_2", "slot1_1", "slot1_2"})


@dataclass(frozen=True)
class ModbusRtuTarget:
    """Connection details for a remote Modbus RTU (serial) device."""

    name: str
    serial_port: str = ""
    device_id: int = 1
    baudrate: int = 9600
    bytesize: int = 8
    parity: str = "N"
    stopbits: int = 1
    timeout_ms: int = 1000
    com_port: str = "cpu2"

    def __post_init__(self) -> None:
        if not isinstance(self.name, str):
            raise TypeError(f"name must be str, got {type(self.name).__name__}")
        if not self.name:
            raise ValueError("name must not be empty")
        if not isinstance(self.serial_port, str):
            raise TypeError(f"serial_port must be str, got {type(self.serial_port).__name__}")
        if not isinstance(self.device_id, int):
            raise TypeError(f"device_id must be int, got {type(self.device_id).__name__}")
        if self.device_id < 0 or self.device_id > 255:
            raise ValueError("device_id must be in 0..255")
        if not isinstance(self.baudrate, int):
            raise TypeError(f"baudrate must be int, got {type(self.baudrate).__name__}")
        if self.baudrate <= 0:
            raise ValueError("baudrate must be > 0")
        if not isinstance(self.bytesize, int):
            raise TypeError(f"bytesize must be int, got {type(self.bytesize).__name__}")
        if self.bytesize not in {5, 6, 7, 8}:
            raise ValueError("bytesize must be in {5, 6, 7, 8}")
        if not isinstance(self.parity, str):
            raise TypeError(f"parity must be str, got {type(self.parity).__name__}")
        if self.parity not in {"N", "E", "O", "M", "S"}:
            raise ValueError("parity must be one of 'N', 'E', 'O', 'M', 'S'")
        if not isinstance(self.stopbits, int):
            raise TypeError(f"stopbits must be int, got {type(self.stopbits).__name__}")
        if self.stopbits not in {1, 2}:
            raise ValueError("stopbits must be 1 or 2")
        if not isinstance(self.timeout_ms, int):
            raise TypeError(f"timeout_ms must be int, got {type(self.timeout_ms).__name__}")
        if self.timeout_ms <= 0:
            raise ValueError("timeout_ms must be > 0")
        if not isinstance(self.com_port, str):
            raise TypeError(f"com_port must be str, got {type(self.com_port).__name__}")
        if self.com_port not in VALID_COM_PORTS:
            raise ValueError(
                f"com_port must be one of {sorted(VALID_COM_PORTS)}, got {self.com_port!r}"
            )


# ---------------------------------------------------------------------------
# Click-specific helpers
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


# ---------------------------------------------------------------------------
# Generic helpers
# ---------------------------------------------------------------------------


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
# Raw Modbus value packing / unpacking
# ---------------------------------------------------------------------------


def _registers_per_tag(tag_type: TagType, register_type: RegisterType) -> int:
    """Return the number of Modbus registers (or coils) consumed by one tag."""
    if register_type in {RegisterType.COIL, RegisterType.DISCRETE_INPUT}:
        return 1
    if tag_type in {TagType.DINT, TagType.REAL}:
        return 2
    return 1


def _calculate_register_count(tag_types: list[TagType], register_type: RegisterType) -> int:
    return sum(_registers_per_tag(tt, register_type) for tt in tag_types)


def _preview_operand_tag_types(operand: Tag | BlockRange, count: int) -> list[TagType]:
    """Get tag types from an operand without needing a ScanContext."""
    if isinstance(operand, Tag):
        return [operand.type]
    return [operand.block.type] * count


def _pack_values_to_registers(
    values: list[Any],
    tags: list[Tag],
    word_order: WordOrder,
    register_type: RegisterType,
) -> list[Any]:
    """Pack tag values into Modbus register (or coil) values for writing."""
    if register_type == RegisterType.COIL:
        return [bool(v) for v in values]
    registers: list[int] = []
    for value, tag in zip(values, tags, strict=True):
        if tag.type == TagType.DINT:
            hi, lo = struct.unpack(">HH", struct.pack(">i", int(value)))
            if word_order == WordOrder.HIGH_LOW:
                registers.extend([hi, lo])
            else:
                registers.extend([lo, hi])
        elif tag.type == TagType.REAL:
            hi, lo = struct.unpack(">HH", struct.pack(">f", float(value)))
            if word_order == WordOrder.HIGH_LOW:
                registers.extend([hi, lo])
            else:
                registers.extend([lo, hi])
        else:
            registers.append(int(value))
    return registers


def _unpack_registers_to_values(
    registers: list[Any],
    tags: list[Tag],
    word_order: WordOrder,
    register_type: RegisterType,
) -> tuple[Any, ...]:
    """Unpack Modbus register (or coil) values into tag-typed values."""
    if register_type in {RegisterType.COIL, RegisterType.DISCRETE_INPUT}:
        return tuple(bool(v) for v in registers[: len(tags)])
    values: list[Any] = []
    idx = 0
    for tag in tags:
        if tag.type in {TagType.DINT, TagType.REAL}:
            if word_order == WordOrder.HIGH_LOW:
                hi, lo = registers[idx], registers[idx + 1]
            else:
                lo, hi = registers[idx], registers[idx + 1]
            raw_bytes = struct.pack(">HH", hi, lo)
            if tag.type == TagType.DINT:
                (val,) = struct.unpack(">i", raw_bytes)
            else:
                (val,) = struct.unpack(">f", raw_bytes)
            values.append(val)
            idx += 2
        else:
            values.append(registers[idx])
            idx += 1
    return tuple(values)


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
    """Modbus send (write to remote device).

    Live I/O is performed when a connection target is set (``host`` for
    Click TCP, ``raw_target`` for raw Modbus).  When neither is set the
    instruction is a simulation-inert placeholder for code generation.
    """

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
