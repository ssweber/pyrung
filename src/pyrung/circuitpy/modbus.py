"""CircuitPython Modbus configuration types."""

from __future__ import annotations

from dataclasses import dataclass

from pyrung.click.send_receive import ModbusTarget


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
        if not isinstance(self.port, int):
            raise TypeError(f"port must be int, got {type(self.port).__name__}")
        if self.port < 1 or self.port > 65535:
            raise ValueError("port must be in 1..65535")
        if not isinstance(self.max_clients, int):
            raise TypeError(f"max_clients must be int, got {type(self.max_clients).__name__}")
        if self.max_clients < 1 or self.max_clients > 7:
            raise ValueError("max_clients must be in 1..7")


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
