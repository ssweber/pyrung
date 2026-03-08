"""CircuitPython Modbus configuration types."""

from __future__ import annotations

from dataclasses import dataclass


def _validate_port(port: int, *, field_name: str) -> None:
    if not isinstance(port, int):
        raise TypeError(f"{field_name} must be int, got {type(port).__name__}")
    if port < 1 or port > 65535:
        raise ValueError(f"{field_name} must be in 1..65535")


@dataclass(frozen=True)
class ModbusServerConfig:
    ip: str
    subnet: str = "255.255.255.0"
    gateway: str = "192.168.1.1"
    dns: str = "0.0.0.0"
    port: int = 502
    max_clients: int = 2

    def __post_init__(self) -> None:
        for field_name in ("ip", "subnet", "gateway", "dns"):
            value = getattr(self, field_name)
            if not isinstance(value, str):
                raise TypeError(f"{field_name} must be str, got {type(value).__name__}")
            if not value:
                raise ValueError(f"{field_name} must not be empty")
        _validate_port(self.port, field_name="port")
        if not isinstance(self.max_clients, int):
            raise TypeError(f"max_clients must be int, got {type(self.max_clients).__name__}")
        if self.max_clients < 1 or self.max_clients > 7:
            raise ValueError("max_clients must be in 1..7")


@dataclass(frozen=True)
class ModbusTarget:
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
        _validate_port(self.port, field_name="port")
        if not isinstance(self.device_id, int):
            raise TypeError(f"device_id must be int, got {type(self.device_id).__name__}")
        if self.device_id < 0 or self.device_id > 255:
            raise ValueError("device_id must be in 0..255")
        if not isinstance(self.timeout_ms, int):
            raise TypeError(f"timeout_ms must be int, got {type(self.timeout_ms).__name__}")
        if self.timeout_ms <= 0:
            raise ValueError("timeout_ms must be > 0")


@dataclass(frozen=True)
class ModbusClientConfig:
    targets: tuple[ModbusTarget, ...]

    def __post_init__(self) -> None:
        if not isinstance(self.targets, tuple):
            raise TypeError(f"targets must be tuple, got {type(self.targets).__name__}")
        if not self.targets:
            raise ValueError("targets must not be empty")
        seen: set[str] = set()
        for target in self.targets:
            if not isinstance(target, ModbusTarget):
                raise TypeError(
                    f"targets must contain ModbusTarget values, got {type(target).__name__}"
                )
            if target.name in seen:
                raise ValueError(f"Duplicate Modbus target name: {target.name!r}")
            seen.add(target.name)


__all__ = [
    "ModbusClientConfig",
    "ModbusServerConfig",
    "ModbusTarget",
]
