"""The text the instruction emitters write for each CLICK behaviour, built in one place.

Each helper returns the source for one piece of an instruction's scan, such as a memory
read or a timer's tick.  The kernel and the CircuitPython ``code.py`` share them, so a
behaviour is written once for both.

Some emitters spell the same piece differently, and the helpers keep each spelling, so
the output does not change.  A ``quote`` of ``"double"`` writes a memory key as
``"key"``, and ``"repr"`` writes it as Python's ``repr`` does (``'key'``).
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Literal

from pyrung.circuitpy.codegen._constants import _DINT_MAX, _DINT_MIN, _INT_MAX
from pyrung.circuitpy.codegen.context import CodegenContext
from pyrung.core.tag import Tag
from pyrung.core.time_mode import TimeUnit

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


def emit_prev(key: str, *, cast: bool = True) -> str:
    """A bit as the instruction memory *key* last stored it.  It reads off until the first store.

    The edge contacts read the cell without ``bool()`` (``cast=False``), and every other site
    wraps it.
    """
    read = emit_mem_get(key, "False")
    return f"bool({read})" if cast else read


def emit_prev_store(key: str, now: str) -> str:
    """A store of this scan's bit *now* into *key*, for the next scan's :func:`emit_prev`."""
    return emit_mem_set(key, now)


def emit_rise(now: str, key: str, *, cast: bool = True, paren: bool = False) -> str:
    """True on the scan the bit *now* is on and *key* holds it off.

    The drum writes ``(not ...)`` in brackets (``paren=True``), and the other sites do not.
    """
    not_prev = f"not {emit_prev(key, cast=cast)}"
    if paren:
        not_prev = f"({not_prev})"
    return f"{now} and {not_prev}"


def _indented(lines: Sequence[str]) -> list[str]:
    return [f"    {line}" for line in lines]


def emit_oneshot(
    key: str,
    enabled: str | bool,
    *,
    fire: Sequence[str],
    off: Sequence[str],
    held: Sequence[str] = (),
    before: Sequence[str] = (),
) -> list[str]:
    """A one-shot that runs *fire* on the first enabled scan, at no indent.

    *off* runs while the rung is off, and *held* runs on each later enabled scan.  *before*
    runs on every enabled scan ahead of the test.  A literal *enabled* (``True`` or
    ``False``) leaves the rung test out.
    """
    if enabled is False:
        return [emit_prev_store(key, "False"), *off]
    fired = [f"if not {emit_prev(key)}:", *_indented([*fire, emit_prev_store(key, "True")])]
    if held:
        fired += ["else:", *_indented(held)]
    if enabled is True:
        return [*before, *fired]
    lines = [f"if not ({enabled}):", *_indented([emit_prev_store(key, "False"), *off])]
    if before:
        return [*lines, "else:", *_indented([*before, *fired])]
    return [*lines, f"el{fired[0]}", *fired[1:]]


def emit_saturate(value: str, high: int, low: int | None = None) -> str:
    """*value* held at or below *high*, and at or above *low* when it is given.

    The timers write ``min(value, high)``, and the counters write
    ``max(low, min(high, value))``.
    """
    if low is None:
        return f"min({value}, {high})"
    return f"max({low}, min({high}, {value}))"


def _carry_lines(acc: Tag, unit: TimeUnit, ctx: CodegenContext) -> list[str]:
    """This scan's time into ``_int_units`` whole units and a ``_new_frac`` remainder.

    Integer nanoseconds, as ``instruction.utils.elapsed_units``.  A board's clock reads them
    directly, and its floats are too short to hold a time.  The kernel has only ``dt``.
    """
    ctx.timer_unit_ns[acc.name] = unit.ns
    if ctx.kernel_runtime:
        dt_ns = f"_dt_to_ns({emit_mem_get('_dt', '0.0', quote='double')})"
    else:
        dt_ns = emit_mem_get("_dt_ns", "0", quote="double")
    return [
        f"_carry = {emit_frac_get(acc, quote='double')} + {dt_ns}",
        f"_int_units = _carry // {unit.ns}",
        f"_new_frac = _carry % {unit.ns}",
    ]


def emit_tick(
    acc: Tag,
    unit: TimeUnit,
    ctx: CodegenContext,
    *,
    read: Literal["local", "inline", "caller"],
    acc_read: str = "",
    max_acc: int = _INT_MAX,
    frac_first: bool = False,
) -> list[str]:
    """Lines that add this scan's time to *acc* in ``_acc``, held at *max_acc*.

    The rest of a unit is left in ``_new_frac`` for the caller to store.  The drum stores it
    before the add (``frac_first``).  *read* says where Acc comes from.  ``"local"`` reads
    *acc_read* into ``_acc`` first, ``"inline"`` reads it inside the add, and ``"caller"``
    adds to an ``_acc`` the caller has read.
    """
    lines = [f"_acc = int({acc_read})"] if read == "local" else []
    lines += _carry_lines(acc, unit, ctx)
    if frac_first:
        lines.append(emit_frac(acc, "_new_frac"))
    start = f"int({acc_read})" if read == "inline" else "_acc"
    lines.append(f"_acc = {emit_saturate(f'{start} + _int_units', max_acc)}")
    return lines


def emit_count(acc_read: str, *, up: str | None, down: str | None) -> list[str]:
    """Lines that step Acc in ``_acc`` by one on each counted edge, held to the DINT range.

    *up* and *down* name the edge locals.  An up counter sums the edges in ``_delta``, and a
    down counter (``up=None``) steps ``_acc`` itself.
    """
    lines = [f"_acc = int({acc_read})"]
    if up is None:
        return [
            *lines,
            f"if {down}:",
            "    _acc -= 1",
            f"_acc = {emit_saturate('_acc', _DINT_MAX, _DINT_MIN)}",
        ]
    lines += ["_delta = 0", f"if {up}:", "    _delta += 1"]
    if down is not None:
        lines += [f"if {down}:", "    _delta -= 1"]
    lines.append(f"_acc = {emit_saturate('_acc + _delta', _DINT_MAX, _DINT_MIN)}")
    return lines


def emit_done(
    done: str,
    acc: str,
    op: str,
    preset: str,
    *,
    preset_key: str | None,
    paren: bool = True,
    then: Sequence[str] = (),
) -> list[str]:
    """Lines that read *preset*, set *done* from ``_acc`` *op* the preset, and store ``_acc``.

    *done* and *acc* are the write targets.  A *preset_key* also stores the preset for the
    prover.  *then* runs just before the compare, where a timer stores its fraction.  The
    off delay writes the compare without brackets (``paren=False``).
    """
    lines = [f"_preset = int({preset})"]
    if preset_key is not None:
        lines.append(emit_mem_set(preset_key, "_preset", quote="double"))
    lines.extend(then)
    compare = f"_acc {op} _preset"
    lines.append(f"{done} = ({compare})" if paren else f"{done} = {compare}")
    lines.append(f"{acc} = _acc")
    return lines


def emit_timer_clear(
    done: str, acc: str, *, frac: Tag | None = None, done_value: str = "False"
) -> list[str]:
    """Lines that set *done* to *done_value* and *acc* to 0, and clear *frac*'s fraction."""
    lines = [emit_frac(frac, "0", quote="double")] if frac is not None else []
    return [*lines, f"{done} = {done_value}", f"{acc} = 0"]
