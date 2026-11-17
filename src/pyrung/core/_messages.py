"""Message text that several modules raise.

Each fault keeps one wording, so the text lives here and every site that raises it
calls the function.
"""

from __future__ import annotations


def internal_error(detail: str) -> str:
    """Return the message for a check that only a bug in pyrung can fail.

    *detail* is the check's own text. A bug report needs it word for word.
    """
    return (
        f"Internal error in pyrung: {detail.rstrip('.')}. "
        "This is a bug in pyrung. Please report it."
    )


# The runner matches this text to tell a program the kernel cannot build from a bug.
NEEDS_MODBUS_CLIENT = "so the code needs a Modbus client"


def needs_modbus_client(call: str) -> str:
    """Return the message for code generation of *call* with no Modbus client configured."""
    return (
        f"The program has a {call}, {NEEDS_MODBUS_CLIENT}. "
        "Pass modbus_client=ModbusClientConfig(...) to generate_circuitpy()."
    )


def read_only_system_point(name: str) -> str:
    """Return the message for a write to the read-only system tag *name*."""
    return f"Tag '{name}' is a read-only system point. You cannot write it."
