"""Register fall-through crossings for the out coil variants: toggle, debounce, flasher, timed.

No variant is "coil == rung".  Toggle flips on an edge.  Debounce and flasher
time in hidden phases.  Timed drops while its rung is still true.  So the
variants must not inherit ``OutCrossing`` (they are not ``OutInstruction``
subclasses).  Their ``forward`` stays ``UNKNOWN``, because ``sp_values`` and
PILOT's multi-target proof trust any exact value that ``forward`` returns.
Their reverses fall through until the prior-scan rung and the phase timers are
representable.
"""

from __future__ import annotations

from pyrung.core.analysis.crossings import BaseCrossing, register
from pyrung.core.instruction.out_variants import (
    DebounceInstruction,
    FlasherInstruction,
    TimedInstruction,
    ToggleInstruction,
)


class OutVariantCrossing(BaseCrossing):
    """Out coil variants fall through; their written values are not exact."""


_OUT_VARIANT = OutVariantCrossing()
register(ToggleInstruction, _OUT_VARIANT)
register(DebounceInstruction, _OUT_VARIANT)
register(FlasherInstruction, _OUT_VARIANT)
register(TimedInstruction, _OUT_VARIANT)
