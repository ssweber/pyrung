"""CircuitPython dialect for pyrung — P1AM-200 target.

Provides hardware configuration for the ProductivityOpen P1AM-200
industrial automation CPU.  Unlike the Click dialect (pre-built blocks),
CircuitPython uses dynamic slot configuration::

    from pyrung.circuitpy import P1AM

    hw = P1AM()
    inputs  = hw.slot(1, "P1-08SIM")
    outputs = hw.slot(2, "P1-08TRS")

    Button = inputs[1]    # LiveInputTag("Slot1.1", BOOL)
    Light  = outputs[1]   # LiveOutputTag("Slot2.1", BOOL)
"""

from typing import Any, cast

from pyrung.circuitpy.catalog import (
    MODULE_CATALOG,
    ChannelGroup,
    ModuleDirection,
    ModuleSpec,
)
from pyrung.circuitpy.hardware import (
    MAX_SLOTS,
    P1AM,
)
from pyrung.circuitpy.codegen import generate_circuitpy
from pyrung.circuitpy.validation import (
    CircuitPyFinding,
    CircuitPyValidationReport,
    ValidationMode,
    validate_circuitpy_program,
)
from pyrung.core.program import Program


def _circuitpy_dialect_validator(program: Program, *, mode: str = "warn", **kwargs: Any) -> Any:
    hw = kwargs.pop("hw", None)
    if hw is not None and not isinstance(hw, P1AM):
        raise TypeError("Program.validate('circuitpy', ...) expects hw=P1AM(...).")
    if mode not in {"warn", "strict"}:
        raise ValueError("Program.validate('circuitpy', ...) mode must be 'warn' or 'strict'.")
    return validate_circuitpy_program(program, hw=hw, mode=cast(ValidationMode, mode))


Program.register_dialect("circuitpy", _circuitpy_dialect_validator)

__all__ = [
    "CircuitPyFinding",
    "CircuitPyValidationReport",
    "ChannelGroup",
    "MAX_SLOTS",
    "MODULE_CATALOG",
    "ModuleDirection",
    "ModuleSpec",
    "P1AM",
    "ValidationMode",
    "generate_circuitpy",
    "validate_circuitpy_program",
]
