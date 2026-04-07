"""Automatically generated module split."""

from __future__ import annotations

import enum
from dataclasses import dataclass

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
