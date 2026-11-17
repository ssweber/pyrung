"""The text the instruction emitters write for each CLICK behaviour, built in one place.

Each helper returns the source for one piece of an instruction's scan, such as a memory
read or a timer's tick.  The kernel and the CircuitPython ``code.py`` share them, so a
behaviour is written once for both.

Some emitters spell the same piece differently.  The helpers keep each spelling, so the
output does not change.  A ``quote`` of ``"double"`` writes a memory key as ``"key"``.
A ``quote`` of ``"repr"`` writes it as Python's ``repr`` does (``'key'``).
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Literal

from pyrung.circuitpy.codegen._constants import _DINT_MAX, _DINT_MIN, _INT_MAX
from pyrung.circuitpy.codegen._util import _bool_literal
from pyrung.circuitpy.codegen.context import CodegenContext
from pyrung.core.tag import Tag
from pyrung.core.time_mode import TimeUnit

Quote = Literal["repr", "double"]


def _key_literal(key: str, quote: Quote) -> str:
    return repr(key) if quote == "repr" else f'"{key}"'


def emit_mem_get(key: str, default: str, *, quote: Quote = "double") -> str:
    """Return the source that reads instruction memory cell *key*, or *default* if unwritten."""
    return f"_mem.get({_key_literal(key, quote)}, {default})"


def emit_mem_set(key: str, value: str, *, quote: Quote = "repr") -> str:
    """Return the source that stores *value* into instruction memory cell *key*."""
    return f"_mem[{_key_literal(key, quote)}] = {value}"


def _frac_key(acc: Tag) -> str:
    """Return the key of the cell that holds the part of a unit that *acc* has not counted yet."""
    return f"_frac:{acc.name}"


def _frac_get(acc: Tag) -> str:
    return emit_mem_get(_frac_key(acc), "0")


def emit_frac(acc: Tag, value: str, *, quote: Quote = "double") -> str:
    return emit_mem_set(_frac_key(acc), value, quote=quote)


def emit_frac_keep(acc: Tag) -> str:
    """Return the source that stores *acc*'s fraction back into its cell.

    A scan that does not tick still writes the fraction.
    """
    return emit_frac(acc, _frac_get(acc))


def emit_prev(key: str, *, cast: bool = True) -> str:
    """Return the source that reads the bit last stored in instruction memory *key*.

    It reads False until the first store.

    Edge contacts and counter inputs read the cell without ``bool()`` (``cast=False``).
    Every other emitter wraps it.
    """
    read = emit_mem_get(key, "False", quote="repr")
    return f"bool({read})" if cast else read


def emit_rise(now: str, key: str, *, cast: bool = True, paren: bool = False) -> str:
    """Return the source that is true on the scan the bit *now* turns on while *key* holds it off.

    The drum writes ``(not ...)`` in brackets (``paren=True``), and the other emitters do not.
    """
    not_prev = f"not {emit_prev(key, cast=cast)}"
    if paren:
        not_prev = f"({not_prev})"
    return f"{now} and {not_prev}"


def _indented(lines: Sequence[str]) -> list[str]:
    return [f"    {line}" for line in lines]


def emit_oneshot(
    key: str,
    enabled: str,
    *,
    fire: Sequence[str],
    off: Sequence[str],
    held: Sequence[str] = (),
    before: Sequence[str] = (),
) -> list[str]:
    """Return the lines of a one-shot that runs *fire* on the first scan *enabled* is on.

    *off* runs while the rung is off, and *held* runs on each later enabled scan.  *before*
    runs on every enabled scan ahead of the test.  A literal ``True`` enable leaves the rung
    test out.  The lines have no indent.
    """
    fired = [f"if not {emit_prev(key)}:", *_indented([*fire, emit_mem_set(key, "True")])]
    if held:
        fired += ["else:", *_indented(held)]
    if _bool_literal(enabled) is True:
        return [*before, *fired]
    lines = [f"if not ({enabled}):", *_indented([emit_mem_set(key, "False"), *off])]
    if before:
        return [*lines, "else:", *_indented([*before, *fired])]
    return [*lines, f"el{fired[0]}", *fired[1:]]


def _saturate(value: str, high: int, low: int | None = None) -> str:
    """Return the source that holds *value* at or below *high*, and at or above *low* if given.

    The timers write ``min(value, high)``. The counters write
    ``max(low, min(high, value))``.
    """
    if low is None:
        return f"min({value}, {high})"
    return f"max({low}, min({high}, {value}))"


def _carry_lines(acc: Tag, unit: TimeUnit, ctx: CodegenContext) -> list[str]:
    """Return the lines that split this scan's time into ``_int_units`` and ``_new_frac``.

    The lines work in integer nanoseconds, as ``instruction.utils.elapsed_units`` does.  A
    board's clock reads integer nanoseconds directly, and a board's floats are too short to
    hold a time.  The kernel has only ``dt``.
    """
    ctx.timer_unit_ns[acc.name] = unit.ns
    if ctx.kernel_runtime:
        dt_ns = f"_dt_to_ns({emit_mem_get('_dt', '0.0')})"
    else:
        dt_ns = emit_mem_get("_dt_ns", "0")
    return [
        f"_carry = {_frac_get(acc)} + {dt_ns}",
        f"_int_units = _carry // {unit.ns}",
        f"_new_frac = _carry % {unit.ns}",
    ]


def emit_tick(
    acc: Tag,
    unit: TimeUnit,
    ctx: CodegenContext,
    *,
    acc_read: str | None,
    inline: bool = False,
    max_acc: int = _INT_MAX,
    frac_first: bool = False,
) -> list[str]:
    """Return the lines that add this scan's time to *acc* in ``_acc``, held at *max_acc*.

    The rest of a unit stays in ``_new_frac`` for the caller to store.  The drum stores
    ``_new_frac`` before the add (``frac_first``).  The tick reads Acc with *acc_read* into
    ``_acc`` first, or inside the add with *inline*.  With no *acc_read*, the caller has
    already read ``_acc``.
    """
    lines = [f"_acc = int({acc_read})"] if acc_read is not None and not inline else []
    lines += _carry_lines(acc, unit, ctx)
    if frac_first:
        lines.append(emit_frac(acc, "_new_frac", quote="repr"))
    start = f"int({acc_read})" if inline else "_acc"
    lines.append(f"_acc = {_saturate(f'{start} + _int_units', max_acc)}")
    return lines


def emit_count(acc_read: str, *, up: str | None, down: str | None) -> list[str]:
    """Return the lines that step Acc in ``_acc`` by one on each counted edge, within DINT range.

    *up* and *down* name the edge locals.  An up counter sums the edges in ``_delta``, and a
    down counter (``up=None``) steps ``_acc`` itself.
    """
    lines = [f"_acc = int({acc_read})"]
    if up is None:
        lines += [f"if {down}:", "    _acc -= 1"]
        return [*lines, f"_acc = {_saturate('_acc', _DINT_MAX, _DINT_MIN)}"]
    lines += ["_delta = 0", f"if {up}:", "    _delta += 1"]
    if down is not None:
        lines += [f"if {down}:", "    _delta -= 1"]
    return [*lines, f"_acc = {_saturate('_acc + _delta', _DINT_MAX, _DINT_MIN)}"]


def emit_done(
    done: str,
    acc: str,
    op: str,
    preset: str,
    *,
    preset_key: str | None,
    frac: Tag | None = None,
    paren: bool = True,
) -> list[str]:
    """Return the lines that read *preset*, set *done* from ``_acc`` *op* it, and store ``_acc``.

    *done* and *acc* are the write targets.  A *preset_key* also stores the preset for the
    prover.  A timer passes its Acc as *frac*, and the lines store that Acc's fraction before
    the compare.  The off delay writes the compare without brackets (``paren=False``).
    """
    lines = [f"_preset = int({preset})"]
    if preset_key is not None:
        lines.append(emit_mem_set(preset_key, "_preset", quote="double"))
    if frac is not None:
        lines.append(emit_frac(frac, "_new_frac"))
    return [*lines, f"{done} = {emit_cmp('_acc', op, '_preset', paren=paren)}", f"{acc} = _acc"]


def emit_acc_rest(done: str, acc: str, *, frac: Tag | None = None) -> list[str]:
    """Return the lines that set *done* to False and *acc* to 0, and clear *frac*'s fraction."""
    lines = [emit_frac(frac, "0")] if frac is not None else []
    return [*lines, f"{done} = False", f"{acc} = 0"]


def emit_cmp(left: str, op: str, right: str, *, paren: bool = True) -> str:
    """Return the compare *left* *op* *right*, in brackets unless ``paren=False``."""
    if op not in ("==", "!=", "<", "<=", ">", ">="):
        raise ValueError(f"Unsupported comparison: {op!r}")
    compare = f"{left} {op} {right}"
    return f"({compare})" if paren else compare


def emit_reached(acc: str, preset: str) -> str:
    """Return the source that is true once a timer's *acc* has reached *preset*."""
    return emit_cmp(acc, ">=", preset, paren=False)


def emit_truth(value: str, *, as_int: bool = False, negate: bool = False) -> str:
    """Return the source that reads *value* as a contact does.

    A bit reads as ``bool(value)``, or ``(not bool(value))`` with *negate*.  A number
    (``as_int``) is true when it is not 0.
    """
    if as_int:
        return emit_cmp(f"int({value})", "!=", "0")
    return f"(not bool({value}))" if negate else f"bool({value})"


def emit_choose(by: str, dst: str, values: Sequence[str]) -> list[str]:
    """Return the lines that set *dst* to the value at place *by* in *values*, counting from 1.

    Any other *by* leaves *dst* as it was.
    """
    lines: list[str] = []
    for place, value in enumerate(values, start=1):
        branch = "if" if place == 1 else "elif"
        lines += [f"{branch} {by} == {place}:", f"    {dst} = {value}"]
    return lines


def emit_if_enabled(
    enabled: str, body: Sequence[str], *, otherwise: Sequence[str] = (), fold: bool = True
) -> list[str]:
    """Return *body* under the rung test *enabled*, and *otherwise* under its ``else``.

    With *fold*, a literal ``True`` enable leaves the test out and keeps only *body*.  The
    timers and the latch and reset coils write ``if True:`` (``fold=False``).
    """
    if fold and _bool_literal(enabled) is True:
        return list(body)
    lines = [f"if {enabled}:", *_indented(body)]
    if otherwise:
        lines += ["else:", *_indented(otherwise)]
    return lines
