"""The text the instruction emitters write for each CLICK behaviour, built in one place.

Each helper returns the source for one piece of an instruction's scan, such as a memory
read or a timer's tick.  The kernel and the CircuitPython ``code.py`` share them, so a
behaviour is written once for both.

Some emitters spell the same piece differently.  The helpers keep each spelling, so the
output does not change.  A ``quote`` of ``"double"`` writes a memory key as ``"key"``.
A ``quote`` of ``"repr"`` writes it as Python's ``repr`` does (``'key'``).

A helper given a ``ctx`` with a recorder also records what its piece reads, writes and
compares (``pyrung.core.relations``).  With no recorder it writes the same text and
builds no record.
"""

from __future__ import annotations

import ast
from collections.abc import Sequence
from typing import Any, Literal

from pyrung.circuitpy.codegen._constants import _DINT_MAX, _DINT_MIN, _INT_MAX
from pyrung.circuitpy.codegen._util import _bool_literal
from pyrung.circuitpy.codegen.context import CodegenContext
from pyrung.core.relations.records import (
    Compare,
    Const,
    Memory,
    Read,
    Recorder,
    Rise,
    Setting,
    Step,
    Store,
    TagValue,
    Term,
    Updated,
)
from pyrung.core.tag import Tag
from pyrung.core.time_mode import TimeUnit

Quote = Literal["repr", "double"]


def _key_literal(key: str, quote: Quote) -> str:
    return repr(key) if quote == "repr" else f'"{key}"'


def _recorder(ctx: CodegenContext | None) -> Recorder | None:
    return None if ctx is None else ctx.recorder


def _literal(text: str) -> Const | None:
    """*text* as a constant when it is a Python literal, such as ``0`` or ``True``."""
    try:
        return Const(ast.literal_eval(text))
    except (ValueError, SyntaxError):
        return None


def emit_mem_get(
    key: str, default: str, *, quote: Quote = "double", ctx: CodegenContext | None = None
) -> str:
    """The instruction memory cell *key*, or *default* while it is unwritten."""
    text = f"_mem.get({_key_literal(key, quote)}, {default})"
    if (rec := _recorder(ctx)) is not None:
        literal = _literal(default)
        text = rec.mark(Read(Memory(key), None if literal is None else literal.value), text)
    return text


def emit_mem_set(
    key: str, value: str, *, quote: Quote = "repr", ctx: CodegenContext | None = None
) -> str:
    """A store of *value* into the instruction memory cell *key*."""
    if (rec := _recorder(ctx)) is not None:
        value = rec.mark(Store(Memory(key), _literal(value)), value)
    return f"_mem[{_key_literal(key, quote)}] = {value}"


def emit_store(
    target: str, value: str, *, ctx: CodegenContext | None, dst: str, term: Term | Compare | None
) -> str:
    """A store of *value* into the tag *dst*, written to *target*.  *term* says what it stores."""
    if (rec := _recorder(ctx)) is not None:
        value = rec.mark(Store(dst, term), value)
    return f"{target} = {value}"


def _frac_key(acc: Tag) -> str:
    """The memory cell holding the part of a unit a timer's *acc* has not counted yet."""
    return f"_frac:{acc.name}"


def _frac_get(acc: Tag, ctx: CodegenContext | None = None) -> str:
    return emit_mem_get(_frac_key(acc), "0", ctx=ctx)


def emit_frac(
    acc: Tag, value: str, *, quote: Quote = "double", ctx: CodegenContext | None = None
) -> str:
    return emit_mem_set(_frac_key(acc), value, quote=quote, ctx=ctx)


def emit_frac_keep(acc: Tag) -> str:
    """A store of *acc*'s fraction back into its cell, so a scan that does not tick writes it."""
    return emit_frac(acc, _frac_get(acc))


def emit_prev(key: str, *, cast: bool = True, ctx: CodegenContext | None = None) -> str:
    """A bit as the instruction memory *key* last stored it.  It reads False until the first store.

    Edge contacts and counter inputs read the cell without ``bool()`` (``cast=False``).
    Every other emitter wraps it.
    """
    read = emit_mem_get(key, "False", quote="repr", ctx=ctx)
    return f"bool({read})" if cast else read


def emit_rise(
    now: str,
    key: str,
    *,
    cast: bool = True,
    paren: bool = False,
    ctx: CodegenContext | None = None,
) -> str:
    """True on the scan the bit *now* is on and *key* holds it off.

    The drum writes ``(not ...)`` in brackets (``paren=True``), and the other emitters do not.
    """
    not_prev = f"not {emit_prev(key, cast=cast, ctx=ctx)}"
    if paren:
        not_prev = f"({not_prev})"
    text = f"{now} and {not_prev}"
    if (rec := _recorder(ctx)) is not None:
        text = rec.mark(Rise(key), text)
    return text


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
    """A one-shot that runs *fire* on the first scan the rung test *enabled* is on.

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
        dt_ns = f"_dt_to_ns({emit_mem_get('_dt', '0.0', ctx=ctx)})"
    else:
        dt_ns = emit_mem_get("_dt_ns", "0", ctx=ctx)
    return [
        f"_carry = {_frac_get(acc, ctx)} + {dt_ns}",
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
    """Lines that add this scan's time to *acc* in ``_acc``, held at *max_acc*.

    The rest of a unit is left in ``_new_frac`` for the caller to store.  The drum stores
    ``_new_frac`` before the add (``frac_first``).  The tick reads Acc with *acc_read* into
    ``_acc`` first, or inside the add with *inline*.  With no *acc_read*, the caller has read
    ``_acc``.
    """
    lines = [f"_acc = int({acc_read})"] if acc_read is not None and not inline else []
    lines += _carry_lines(acc, unit, ctx)
    if frac_first:
        lines.append(emit_frac(acc, "_new_frac", quote="repr", ctx=ctx))
    start = f"int({acc_read})" if inline else "_acc"
    added = _saturate(f"{start} + _int_units", max_acc)
    if (rec := _recorder(ctx)) is not None:
        added = rec.mark(Step(acc.name, None, None, max_acc), added)
    lines.append(f"_acc = {added}")
    return lines


def emit_count(
    acc: Tag, acc_read: str, *, up: str | None, down: str | None, ctx: CodegenContext
) -> list[str]:
    """Lines that step Acc in ``_acc`` by one on each counted edge, held to the DINT range.

    *up* and *down* name the edge locals.  An up counter sums the edges in ``_delta``, and a
    down counter (``up=None``) steps ``_acc`` itself.
    """

    def one(delta: int) -> str:
        if (rec := _recorder(ctx)) is not None:
            return rec.mark(Step(acc.name, delta, _DINT_MIN, _DINT_MAX), "1")
        return "1"

    lines = [f"_acc = int({acc_read})"]
    if up is None:
        lines += [f"if {down}:", f"    _acc -= {one(-1)}"]
        return [*lines, f"_acc = {_saturate('_acc', _DINT_MAX, _DINT_MIN)}"]
    lines += ["_delta = 0", f"if {up}:", f"    _delta += {one(1)}"]
    if down is not None:
        lines += [f"if {down}:", f"    _delta -= {one(-1)}"]
    return [*lines, f"_acc = {_saturate('_acc + _delta', _DINT_MAX, _DINT_MIN)}"]


def _operand(owner: Any, field: str) -> Term:
    name = getattr(getattr(owner, field), "name", None)
    return Setting(field) if name is None else TagValue(name)


def emit_done(
    done: str,
    acc: str,
    op: str,
    preset: str,
    *,
    owner: Any,
    ctx: CodegenContext,
    preset_key: str | None,
    frac: bool = False,
    paren: bool = True,
) -> list[str]:
    """Lines that read *preset*, set *done* from ``_acc`` *op* the preset, and store ``_acc``.

    *done* and *acc* are the write targets of the timer or counter *owner*.  A *preset_key*
    also stores the preset for the prover.  A timer passes *frac*, and the lines store its
    Acc's fraction before the compare.  The off delay writes the compare without brackets
    (``paren=False``).
    """
    acc_name = owner.accumulator.name
    lines = [f"_preset = int({preset})"]
    if preset_key is not None:
        lines.append(emit_mem_set(preset_key, "_preset", quote="double"))
    if frac:
        lines.append(emit_frac(owner.accumulator, "_new_frac", ctx=ctx))
    compare = Compare(Updated(acc_name), op, _operand(owner, "preset"))
    done_value = emit_cmp("_acc", op, "_preset", paren=paren)
    return [
        *lines,
        emit_store(done, done_value, ctx=ctx, dst=owner.done_bit.name, term=compare),
        emit_store(acc, "_acc", ctx=ctx, dst=acc_name, term=Updated(acc_name)),
    ]


def emit_acc_rest(
    done: str,
    acc: str,
    *,
    owner: Any,
    ctx: CodegenContext,
    frac: bool = False,
    done_value: bool = False,
) -> list[str]:
    """Lines that set *done* to *done_value* and *acc* to 0, and clear the fraction (*frac*)."""
    lines = [emit_frac(owner.accumulator, "0", ctx=ctx)] if frac else []
    done_text = str(done_value)
    return [
        *lines,
        emit_store(done, done_text, ctx=ctx, dst=owner.done_bit.name, term=Const(done_value)),
        emit_store(acc, "0", ctx=ctx, dst=owner.accumulator.name, term=Const(0)),
    ]


def emit_cmp(
    left: str,
    op: str,
    right: str,
    *,
    paren: bool = True,
    ctx: CodegenContext | None = None,
    terms: tuple[Term, Term] | None = None,
) -> str:
    """The compare *left* *op* *right*, in brackets unless ``paren=False``.

    *terms* say what *left* and *right* hold, for the recorder.
    """
    if op not in ("==", "!=", "<", "<=", ">", ">="):
        raise ValueError(f"Unsupported comparison: {op!r}")
    compare = f"{left} {op} {right}"
    text = f"({compare})" if paren else compare
    if terms is not None and (rec := _recorder(ctx)) is not None:
        text = rec.mark(Compare(terms[0], op, terms[1]), text)
    return text


def emit_reached(acc: str, preset: str) -> str:
    """True once a timer's *acc* has reached *preset*."""
    return emit_cmp(acc, ">=", preset, paren=False)


def emit_truth(value: str, *, as_int: bool = False, negate: bool = False) -> str:
    """*value* as a contact reads it.

    A bit reads as ``bool(value)``, or ``(not bool(value))`` with *negate*.  A number
    (``as_int``) is true when it is not 0.
    """
    if as_int:
        return emit_cmp(f"int({value})", "!=", "0")
    return f"(not bool({value}))" if negate else f"bool({value})"


def emit_choose(by: str, dst: str, values: Sequence[str]) -> list[str]:
    """Lines that set *dst* to the value at place *by* in *values*, counting from 1.

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
    """*body* under the rung test *enabled*, and *otherwise* under its ``else``.

    With *fold*, a literal ``True`` enable leaves the test out and keeps only *body*.  The
    timers and the latch and reset coils write ``if True:`` (``fold=False``).
    """
    if fold and _bool_literal(enabled) is True:
        return list(body)
    lines = [f"if {enabled}:", *_indented(body)]
    if otherwise:
        lines += ["else:", *_indented(otherwise)]
    return lines
