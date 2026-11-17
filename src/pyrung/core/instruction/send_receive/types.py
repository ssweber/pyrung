"""Automatically generated module split."""

from __future__ import annotations

import enum
from dataclasses import dataclass, field
from typing import Literal

from pyrung.core.tag import Tag, TagType

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

    ``notation`` records which form was written, so CLICK export can keep it.
    It takes no part in equality: ``"64h"`` and ``400101`` are the same register.
    """

    address: int
    register_type: RegisterType = RegisterType.HOLDING
    notation: Literal["984", "hex", "raw"] = field(
        default="raw", init=False, compare=False, repr=False
    )

    def __post_init__(self) -> None:
        addr = self.address
        # --- hex string with "h" suffix (e.g. "0h", "FFFEh") ---
        if isinstance(addr, str):
            object.__setattr__(self, "notation", "hex")
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
            object.__setattr__(self, "notation", "984")
            if self.register_type != RegisterType.HOLDING:
                # Caller explicitly set register_type — must match
                if self.register_type != inferred_rt:
                    raise ValueError(
                        f"Address {addr} is a {inferred_rt.name} address, but you passed "
                        f"register_type={self.register_type.name}. Leave register_type out, "
                        f"or set it to {inferred_rt.name}."
                    )
            else:
                object.__setattr__(self, "register_type", inferred_rt)
            addr = raw_addr
        # --- range check on resolved raw address ---
        if addr < 0 or addr > 0xFFFE:
            raise ValueError(
                f"Modbus address {addr} is out of range. Use 0..0xFFFE, a 984 address such "
                "as 400001, or a hex string such as '0h'."
            )
        if not isinstance(self.register_type, RegisterType):
            raise TypeError(
                f"register_type must be RegisterType, got {type(self.register_type).__name__}"
            )


# ---------------------------------------------------------------------------
# Target dataclasses
# ---------------------------------------------------------------------------


def _check_device_id(device_id: object, high: int) -> None:
    """CLICK's Slave ID box takes a number or a DS register. It refuses DD, DH and C."""
    if isinstance(device_id, Tag):
        if device_id.type is not TagType.INT:
            raise TypeError(
                f"device_id must be an int 0..{high} or a DS tag (an Int), "
                f"got {device_id.type.name.title()} tag {device_id.name!r}"
            )
        return
    if isinstance(device_id, bool) or not isinstance(device_id, int):
        raise TypeError(
            f"device_id must be an int 0..{high} or a DS tag (an Int), "
            f"got {type(device_id).__name__}"
        )
    if device_id < 0 or device_id > high:
        raise ValueError(f"device_id must be an int 0..{high} or a DS tag, got {device_id}")


class _TargetEquality:
    """Compare and hash a target by its fields, with a tag ``device_id`` taken by type and name.

    A tag's own ``==`` builds a rung condition, so the dataclass ``==`` would raise on it.
    """

    def _key(self) -> tuple[object, ...]:
        return tuple(
            (value.type, value.name) if isinstance(value, Tag) else value
            for value in vars(self).values()
        )

    def __eq__(self, other: object) -> bool:
        if type(other) is not type(self):
            return NotImplemented
        return self._key() == other._key()

    def __hash__(self) -> int:
        return hash((type(self), self._key()))


VALID_TCP_COM_PORTS = frozenset({"ethernet", "wlan"})


@dataclass(frozen=True, eq=False)
class ModbusTcpTarget(_TargetEquality):
    """Connection details for a remote Modbus TCP device.

    ``device_id`` is a number or a DS tag that holds it.  ``com_port`` is
    ``"ethernet"`` (the default) or ``"wlan"``.
    """

    name: str
    ip: str
    port: int = 502
    device_id: int | Tag = 1
    timeout_ms: int = 1000
    com_port: str = "ethernet"

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
            raise ValueError(f"port must be in 1..65535, got {self.port}.")
        _check_device_id(self.device_id, 255)
        if not isinstance(self.timeout_ms, int):
            raise TypeError(f"timeout_ms must be int, got {type(self.timeout_ms).__name__}")
        if self.timeout_ms <= 0:
            raise ValueError(f"timeout_ms must be > 0, got {self.timeout_ms}.")
        if not isinstance(self.com_port, str):
            raise TypeError(f"com_port must be str, got {type(self.com_port).__name__}")
        if self.com_port not in VALID_TCP_COM_PORTS:
            raise ValueError(
                f"com_port must be one of {sorted(VALID_TCP_COM_PORTS)}, got {self.com_port!r}"
            )


VALID_COM_PORTS = frozenset({"cpu2", "cpu3", "slot0_1", "slot0_2", "slot1_1", "slot1_2"})


@dataclass(frozen=True, eq=False)
class ModbusRtuTarget(_TargetEquality):
    """Connection details for a remote Modbus RTU (serial) device.

    ``device_id`` is a number 0..247 or a DS tag that holds it.
    """

    name: str
    serial_port: str = ""
    device_id: int | Tag = 1
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
        # Modbus RTU unit addresses stop at 247, so 248..255 reach no device.
        _check_device_id(self.device_id, 247)
        if not isinstance(self.baudrate, int):
            raise TypeError(f"baudrate must be int, got {type(self.baudrate).__name__}")
        if self.baudrate <= 0:
            raise ValueError(f"baudrate must be > 0, got {self.baudrate}.")
        if not isinstance(self.bytesize, int):
            raise TypeError(f"bytesize must be int, got {type(self.bytesize).__name__}")
        if self.bytesize not in {5, 6, 7, 8}:
            raise ValueError(f"bytesize must be 5, 6, 7 or 8, got {self.bytesize}.")
        if not isinstance(self.parity, str):
            raise TypeError(f"parity must be str, got {type(self.parity).__name__}")
        if self.parity not in {"N", "E", "O", "M", "S"}:
            raise ValueError(f"parity must be one of 'N', 'E', 'O', 'M', 'S', got {self.parity!r}.")
        if not isinstance(self.stopbits, int):
            raise TypeError(f"stopbits must be int, got {type(self.stopbits).__name__}")
        if self.stopbits not in {1, 2}:
            raise ValueError(f"stopbits must be 1 or 2, got {self.stopbits}.")
        if not isinstance(self.timeout_ms, int):
            raise TypeError(f"timeout_ms must be int, got {type(self.timeout_ms).__name__}")
        if self.timeout_ms <= 0:
            raise ValueError(f"timeout_ms must be > 0, got {self.timeout_ms}.")
        if not isinstance(self.com_port, str):
            raise TypeError(f"com_port must be str, got {type(self.com_port).__name__}")
        if self.com_port not in VALID_COM_PORTS:
            raise ValueError(
                f"com_port must be one of {sorted(VALID_COM_PORTS)}, got {self.com_port!r}"
            )
