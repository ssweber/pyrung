from __future__ import annotations

from collections.abc import Callable
from typing import overload

from .context import Program
from .validation import _check_function_body_strict


@overload
def program(fn: Callable[[], None], /) -> Program: ...


@overload
def program(
    fn: None = None,
    /,
    *,
    strict: bool = True,
) -> Callable[[Callable[[], None]], Program]: ...


def program(
    fn: Callable[[], None] | None = None,
    /,
    *,
    strict: bool = True,
) -> Program | Callable[[Callable[[], None]], Program]:
    """Decorator to create a Program from a function.

    Example:
        @program
        def my_logic():
            with Rung(Button):
                out(Light)

        @program(strict=False)
        def permissive_logic():
            with Rung(Button):
                out(Light)

        runner = PLCRunner(my_logic)
    """

    def _decorate(inner_fn: Callable[[], None]) -> Program:
        if strict:
            _check_function_body_strict(
                inner_fn,
                opt_out_hint="@program(strict=False)",
                source_label=f"@program {getattr(inner_fn, '__qualname__', repr(inner_fn))}",
            )
        prog = Program(strict=strict)
        with prog:
            inner_fn()
        return prog

    if fn is None:
        return _decorate
    return _decorate(fn)
