"""The text the instruction emitters write for each CLICK behaviour, built in one place.

Each helper returns the source for one piece of an instruction's scan, such as a memory
read or a timer's tick.  The kernel and the CircuitPython ``code.py`` share them, so a
behaviour is written once for both.

Some emitters spell the same piece differently, and the helpers keep each spelling, so
the output does not change.  A ``quote`` of ``"double"`` writes a memory key as
``"key"``, and ``"repr"`` writes it as Python's ``repr`` does (``'key'``).
"""

from __future__ import annotations

from typing import Literal

from pyrung.core.tag import Tag

Quote = Literal["repr", "double"]


def _key_literal(key: str, quote: Quote) -> str:
    return repr(key) if quote == "repr" else f'"{key}"'


def emit_mem_get(key: str, default: str, *, quote: Quote = "repr") -> str:
    """The instruction memory cell *key*, or *default* while it is unwritten."""
    return f"_mem.get({_key_literal(key, quote)}, {default})"


def emit_mem_set(key: str, value: str, *, quote: Quote = "repr") -> str:
    """A store of *value* into the instruction memory cell *key*."""
    return f"_mem[{_key_literal(key, quote)}] = {value}"


def frac_key(acc: Tag) -> str:
    """The memory cell holding the part of a unit a timer's *acc* has not counted yet."""
    return f"_frac:{acc.name}"


def emit_frac_get(acc: Tag, *, quote: Quote = "repr") -> str:
    return emit_mem_get(frac_key(acc), "0", quote=quote)


def emit_frac(acc: Tag, value: str, *, quote: Quote = "repr") -> str:
    return emit_mem_set(frac_key(acc), value, quote=quote)


def emit_frac_keep(acc: Tag) -> str:
    """A store of *acc*'s fraction back into its cell, so a scan that does not tick writes it."""
    return emit_frac(acc, emit_frac_get(acc, quote="double"), quote="double")
