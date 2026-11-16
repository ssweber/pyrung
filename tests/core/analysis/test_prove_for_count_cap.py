"""Only the static facts' kernel caps a For count.

A Dint count is read whole and signed (``control.for_loop_iterations``), so a
positive one can run up to 2147483647 iterations in one scan.  The facts'
kernel stops a 32-bit count past ``PROVE_FOR_ITERATION_CAP`` so that building
the facts ends.  The runtime's kernel and a code.py run the count as read.
"""

from __future__ import annotations

from collections.abc import Callable

from pyrung.circuitpy.codegen import compile_kernel
from pyrung.core import (
    Bool,
    Dint,
    Program,
    Rung,
    call,
    copy,
    forloop,
    out,
    subroutine,
)


def _loop_program(count: Callable[[], object], before: Callable[[object], None] | None = None):
    btn = Bool("Btn", external=True)
    y2 = Bool("Y2")
    y3 = Bool("Y3")
    n = count()
    with Program(strict=False) as logic:
        if before is not None:
            before(n)
        with Rung():
            with forloop(n):
                call("work")
        with Rung(y2):
            out(y3)
        with subroutine("work"):
            with Rung():
                copy(btn, y2)
    return logic


def test_only_the_facts_kernel_is_capped() -> None:
    # The runtime's kernel (CompiledPLC) and a code.py run the count as read.
    logic = _loop_program(lambda: Dint("N", external=True))
    assert "_ForIterationsBeyondCap" in compile_kernel(logic, proof_metadata=True).source
    assert "_ForIterationsBeyondCap" not in compile_kernel(logic).source
