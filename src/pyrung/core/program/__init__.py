"""Program and Rung context managers for the immutable PLC engine.

Provides DSL syntax for building PLC programs:

    with Program() as logic:
        with Rung(Button):
            out(Light)

    runner = PLCRunner(logic)
"""

from .builders import (
    CountDownBuilder,
    CountUpBuilder,
    OffDelayBuilder,
    OnDelayBuilder,
    ShiftBuilder,
    count_down,
    count_up,
    off_delay,
    on_delay,
    shift,
)
from .conditions import (
    all_of,
    any_of,
    fall,
    nc,
    rise,
)
from .context import (
    Branch,
    ForLoop,
    Program,
    Rung,
    RungContext,
    Subroutine,
    SubroutineFunc,
    branch,
    forloop,
    subroutine,
)
from .decorators import program
from .instructions import (
    blockcopy,
    call,
    copy,
    fill,
    latch,
    math,
    out,
    pack_bits,
    pack_text,
    pack_words,
    reset,
    return_,
    run_enabled_function,
    run_function,
    search,
    unpack_to_bits,
    unpack_to_words,
)
from .validation import (
    DialectValidator,
    ForbiddenControlFlowError,
)

__all__ = [
    # Contexts & Structure
    "Branch",
    "ForLoop",
    "Program",
    "Rung",
    "RungContext",
    "Subroutine",
    "SubroutineFunc",
    # Decorators & Factory Functions
    "branch",
    "forloop",
    "program",
    "subroutine",
    # Conditions
    "all_of",
    "any_of",
    "fall",
    "nc",
    "rise",
    # Basic Instructions
    "call",
    "copy",
    "latch",
    "out",
    "reset",
    "return_",
    # Advanced Instructions
    "blockcopy",
    "fill",
    "math",
    "pack_bits",
    "pack_text",
    "pack_words",
    "run_enabled_function",
    "run_function",
    "search",
    "unpack_to_bits",
    "unpack_to_words",
    # Builders & Terminal Instructions
    "CountDownBuilder",
    "CountUpBuilder",
    "OffDelayBuilder",
    "OnDelayBuilder",
    "ShiftBuilder",
    "count_down",
    "count_up",
    "off_delay",
    "on_delay",
    "shift",
    # Validation & Types
    "DialectValidator",
    "ForbiddenControlFlowError",
]
