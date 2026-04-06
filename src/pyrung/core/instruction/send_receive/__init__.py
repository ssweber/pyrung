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

from ._core import (
    ModbusReceiveInstruction,
    ModbusSendInstruction,
    receive,
    send,
)
from .types import (
    VALID_COM_PORTS,
    ModbusAddress,
    ModbusRtuTarget,
    ModbusTcpTarget,
    RegisterType,
    WordOrder,
)

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
