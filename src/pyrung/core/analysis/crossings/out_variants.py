"""Out coil variant crossings (toggle / debounce / flasher / timed) — fall through.

None of the variants is "coil == rung": toggle flips on an edge, debounce and
flasher time in hidden phases, timed drops while its rung is still true.  So
they must not inherit ``OutCrossing`` (they are not ``OutInstruction``
subclasses), and their ``forward`` stays ``UNKNOWN``: ``sp_values`` and PILOT's
multi-target proof trust any exact value it returns.  Their reverses fall
through until the prior-scan rung and phase timers are representable.
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
