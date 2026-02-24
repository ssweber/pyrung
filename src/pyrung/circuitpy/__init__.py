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

__all__ = [
    "ChannelGroup",
    "MAX_SLOTS",
    "MODULE_CATALOG",
    "ModuleDirection",
    "ModuleSpec",
    "P1AM",
]
