"""pyrung's checks of send() and receive()."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from pyrung.core.instruction.send_receive import is_modbus_status_tag
from pyrung.core.tag import Tag
from pyrung.core.validation.walker import ProgramLocation

from ..checks import Check
from ..findings import (
    CLK_BANK_WRONG_ROLE,
    ClickFinding,
    ValidationMode,
    _route_severity,
)
from ..resolve import (
    _bank_label,
    _format_location,
    _instruction_location,
    _ResolutionCache,
    _unresolved_finding,
)

if TYPE_CHECKING:
    pass


# The banks that each Send or Receive operand accepts, from CLICK's help pages for them
# (the MODBUS TCP and RTU pages agree). No conformance case runs them, so they are not
# probed on hardware. The help pages accept the whole SD bank as a Receive destination,
# so this does not apply copy()'s writable SD subset.
_MODBUS_FLAG_BANKS = frozenset({"C"})


_MODBUS_EXCEPTION_BANKS = frozenset({"DS", "DD"})


_MODBUS_DEVICE_ID_BANKS = frozenset({"DS"})


_MODBUS_RECEIVE_DEST_BANKS = frozenset(
    {"Y", "C", "CTD", "DS", "DD", "DH", "DF", "SD", "TD", "YD", "TXT"}
)


_MODBUS_BANKS: dict[tuple[str, str], frozenset[str]] = {
    ("ModbusReceiveInstruction", "dest"): _MODBUS_RECEIVE_DEST_BANKS,
    ("ModbusReceiveInstruction", "receiving"): _MODBUS_FLAG_BANKS,
    ("ModbusReceiveInstruction", "success"): _MODBUS_FLAG_BANKS,
    ("ModbusReceiveInstruction", "error"): _MODBUS_FLAG_BANKS,
    ("ModbusReceiveInstruction", "exception_response"): _MODBUS_EXCEPTION_BANKS,
    ("ModbusSendInstruction", "sending"): _MODBUS_FLAG_BANKS,
    ("ModbusSendInstruction", "success"): _MODBUS_FLAG_BANKS,
    ("ModbusSendInstruction", "error"): _MODBUS_FLAG_BANKS,
    ("ModbusSendInstruction", "exception_response"): _MODBUS_EXCEPTION_BANKS,
    # CLICK's Slave ID box accepts a number or a DS address.
    ("ModbusReceiveInstruction", "device_id"): _MODBUS_DEVICE_ID_BANKS,
    ("ModbusSendInstruction", "device_id"): _MODBUS_DEVICE_ID_BANKS,
}


def _evaluate_modbus_banks(
    instruction: Any,
    base_location: ProgramLocation,
    mode: ValidationMode,
    resolver: _ResolutionCache,
) -> list[ClickFinding]:
    findings: list[ClickFinding] = []
    instruction_type = type(instruction).__name__

    for (candidate_type, field_name), banks in _MODBUS_BANKS.items():
        if instruction_type != candidate_type:
            continue

        value = getattr(instruction, field_name)
        # If the caller did not pass a status bit, pyrung gives it a hidden tag.  That tag
        # never reaches the CLICK project.
        if isinstance(value, Tag) and is_modbus_status_tag(value.name):
            continue
        # A numeric device ID has no bank.
        if isinstance(value, int):
            continue

        location = _instruction_location(base_location, f"instruction.{field_name}")
        resolution = resolver.resolve_operand_slots(value)
        if resolution.unresolved:
            findings.append(_unresolved_finding(location, mode, "operand"))
            continue

        for slot in resolution.slots:
            if slot.memory_type in banks:
                continue
            location_text = _format_location(location)
            choices = ", ".join(sorted(banks))
            findings.append(
                ClickFinding(
                    code=CLK_BANK_WRONG_ROLE,
                    severity=_route_severity(CLK_BANK_WRONG_ROLE, mode),
                    message=(
                        f"Bank {_bank_label(slot)} is invalid for {field_name} "
                        f"at {location_text}; CLICK takes {choices}."
                    ),
                    location=location_text,
                )
            )

    return findings


CODES: tuple[str, ...] = ()

CHECKS = (
    Check(
        "profile",
        60,
        lambda instruction, location, context: _evaluate_modbus_banks(
            instruction, location, context.mode, context.resolver
        ),
    ),
)

TABLES: dict[str, dict[Any, Any]] = {"MODBUS_BANKS": _MODBUS_BANKS}
