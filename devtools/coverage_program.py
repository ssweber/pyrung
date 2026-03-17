"""Coverage program generator for laddercodec.

Programmatically generates one rung per instruction/operand variant,
plus one rung per condition kind. Auto-allocates Click addresses.

Each rung exercises exactly one codec path so laddercodec can compare
per-rung bytes independently. Rung comments encode stable IDs.

Usage:
    python devtools/coverage_program.py
"""

from __future__ import annotations

from itertools import count
from pathlib import Path
from typing import Any

from pyrung import (
    Block,
    Bool,
    Char,
    Dint,
    Int,
    Program,
    Real,
    Rung,
    TagType,
    Tms,
    Word,
    any_of,
    as_ascii,
    as_binary,
    as_text,
    as_value,
    blockcopy,
    calc,
    copy,
    count_down,
    count_up,
    event_drum,
    fall,
    fill,
    forloop,
    immediate,
    latch,
    off_delay,
    on_delay,
    out,
    pack_bits,
    pack_text,
    pack_words,
    reset,
    rise,
    search,
    shift,
    time_drum,
    unpack_to_bits,
    unpack_to_words,
)
from pyrung.click import (
    ModbusTarget,
    TagMap,
    c,
    ct,
    ctd,
    dd,
    df,
    dh,
    ds,
    send,
    receive,
    t,
    td,
    txt,
    x,
    y,
)


# ── Auto-allocator ──────────────────────────────────────────────────


class Alloc:
    """Sequential Click address allocator with automatic TagMap building."""

    def __init__(self) -> None:
        self._pos: dict[str, int] = {
            b: 1
            for b in ("c", "ds", "dd", "dh", "df", "txt", "x", "y", "t", "td", "ct", "ctd")
        }
        self._seq = count(1)
        self._map: dict[Any, Any] = {}

    def _take(self, bank: str, n: int = 1) -> int:
        start = self._pos[bank]
        self._pos[bank] += n
        return start

    def _name(self) -> str:
        return f"v{next(self._seq):03d}"

    # — single tags —

    def bool(self) -> Bool:
        tag = Bool(self._name())
        self._map[tag] = c[self._take("c")]
        return tag

    def x_bool(self) -> Bool:
        tag = Bool(self._name())
        self._map[tag] = x[self._take("x")]
        return tag

    def y_bool(self) -> Bool:
        tag = Bool(self._name())
        self._map[tag] = y[self._take("y")]
        return tag

    def int_(self) -> Int:
        tag = Int(self._name())
        self._map[tag] = ds[self._take("ds")]
        return tag

    def dint(self) -> Dint:
        tag = Dint(self._name())
        self._map[tag] = dd[self._take("dd")]
        return tag

    def real(self) -> Real:
        tag = Real(self._name())
        self._map[tag] = df[self._take("df")]
        return tag

    def word(self) -> Word:
        tag = Word(self._name())
        self._map[tag] = dh[self._take("dh")]
        return tag

    def char(self) -> Char:
        tag = Char(self._name())
        self._map[tag] = txt[self._take("txt")]
        return tag

    # — timer / counter pairs —

    def timer(self) -> tuple[Bool, Int]:
        n = self._name()
        done, acc = Bool(f"{n}_d"), Int(f"{n}_a")
        self._map[done] = t[self._take("t")]
        self._map[acc] = td[self._take("td")]
        return done, acc

    def counter(self) -> tuple[Bool, Dint]:
        n = self._name()
        done, acc = Bool(f"{n}_d"), Dint(f"{n}_a")
        self._map[done] = ct[self._take("ct")]
        self._map[acc] = ctd[self._take("ctd")]
        return done, acc

    # — blocks —

    def bool_block(self, size: int) -> Block:
        blk = Block(self._name(), TagType.BOOL, 1, size)
        s = self._take("c", size)
        self._map[blk] = c.select(s, s + size - 1)
        return blk

    def int_block(self, size: int) -> Block:
        blk = Block(self._name(), TagType.INT, 1, size)
        s = self._take("ds", size)
        self._map[blk] = ds.select(s, s + size - 1)
        return blk

    def char_block(self, size: int) -> Block:
        blk = Block(self._name(), TagType.CHAR, 1, size)
        s = self._take("txt", size)
        self._map[blk] = txt.select(s, s + size - 1)
        return blk

    def tagmap(self) -> TagMap:
        return TagMap(self._map)


# ── Variant tables ──────────────────────────────────────────────────
#
# Each table is a list of (label, factory) pairs.  The factory receives
# the allocator and returns the operand(s) needed for that variant.
# Adding a new variant = one new line in the table.

# fmt: off

# Condition variants — each factory returns a tuple of Rung args.
CONDITIONS: list[tuple[str, Any]] = [
    ("no",        lambda a: (a.bool(),)),
    ("nc",        lambda a: (~a.bool(),)),
    ("rise",      lambda a: (rise(a.bool()),)),
    ("fall",      lambda a: (fall(a.bool()),)),
    ("immediate", lambda a: (immediate(a.x_bool()),)),
    ("eq",        lambda a: (a.int_() == 5,)),
    ("ne",        lambda a: (a.int_() != 0,)),
    ("lt",        lambda a: (a.int_() < 100,)),
    ("gt",        lambda a: (a.int_() > 0,)),
    ("le",        lambda a: (a.int_() <= 50,)),
    ("ge",        lambda a: (a.int_() >= 10,)),
    ("or",        lambda a: (any_of(a.bool(), a.bool()),)),
    ("and",       lambda a: (a.bool(), a.bool())),
]

# Coil target variants — each factory returns the target operand.
COIL_TARGETS: list[tuple[str, Any]] = [
    ("tag",       lambda a: a.bool()),
    ("block",     lambda a: a.bool_block(4).select(1, 4)),
    ("immediate", lambda a: immediate(a.y_bool())),
]

# Copy source variants — each factory returns (source, dest).
COPY_VARIANTS: list[tuple[str, Any]] = [
    ("tag",           lambda a: (a.int_(),                    a.int_())),
    ("literal_int",   lambda a: (42,                          a.int_())),
    ("literal_float", lambda a: (3.14,                        a.real())),
    ("as_value",      lambda a: (as_value(a.int_()),          a.int_())),
    ("as_text",       lambda a: (as_text(a.int_()),           a.int_())),
    ("as_text_opts",  lambda a: (as_text(a.int_(), suppress_zero=False, pad=3), a.int_())),
    ("as_binary",     lambda a: (as_binary(a.int_()),         a.int_())),
    ("as_ascii",      lambda a: (as_ascii(a.int_()),          a.int_())),
]

# Blockcopy source variants — each factory returns (source_range, dest_range).
BLOCKCOPY_VARIANTS: list[tuple[str, Any]] = [
    ("block",    lambda a: (a.int_block(4).select(1, 4),              a.int_block(4).select(1, 4))),
    ("as_value", lambda a: (as_value(a.int_block(4).select(1, 4)),    a.int_block(4).select(1, 4))),
]

# Fill value variants — each factory returns (value, dest_range).
FILL_VARIANTS: list[tuple[str, Any]] = [
    ("literal", lambda a: (0,        a.int_block(4).select(1, 4))),
    ("tag",     lambda a: (a.int_(), a.int_block(4).select(1, 4))),
]

# Search operator variants.
SEARCH_OPS: list[tuple[str, str]] = [
    ("eq", "=="), ("ne", "!="), ("lt", "<"), ("gt", ">"), ("le", "<="), ("ge", ">="),
]

# Calc variants — each factory returns (expression, dest).
CALC_VARIANTS: list[tuple[str, Any]] = [
    ("decimal", lambda a: (a.int_() + a.int_(), a.int_(),  False)),
    ("hex",     lambda a: (a.word() + a.word(), a.word(),   False)),
    ("oneshot", lambda a: (a.int_() * a.int_(), a.int_(),   True)),
]

# Timer variants — each factory returns (done, acc, preset, unit, reset_tag|None).
TIMER_VARIANTS: list[tuple[str, Any]] = [
    ("ton",  lambda a: ("on",  a.timer(), 3000, None)),          # TON: no reset
    ("rton", lambda a: ("on",  a.timer(), 5000, a.bool())),      # RTON: with reset
    ("tof",  lambda a: ("off", a.timer(), 2000, None)),          # TOF: off-delay
]

# Counter variants — each factory returns (kind, done, acc, preset, down|None, reset).
COUNTER_VARIANTS: list[tuple[str, Any]] = [
    ("up__reset", lambda a: ("up",   a.counter(), 100, None,     a.bool())),
    ("up__down",  lambda a: ("up",   a.counter(), 100, a.bool(), a.bool())),
    ("down",      lambda a: ("down", a.counter(), 50,  None,     a.bool())),
]

# Forloop variants — each factory returns (count, oneshot).
FORLOOP_VARIANTS: list[tuple[str, Any]] = [
    ("basic",   lambda _: (4, False)),
    ("oneshot", lambda _: (4, True)),
]

# fmt: on


# ── Program generation ──────────────────────────────────────────────

a = Alloc()

with Program() as coverage_program:

    # ── 1. Condition coverage (each kind × simple out) ──────────────

    for label, make_cond in CONDITIONS:
        target = a.bool()
        with Rung(*make_cond(a)) as r:
            r.comment = f"cond__{label}"
            out(target)

    # ── 2. Coil instructions (out/latch/reset × target kinds) ───────

    for func_name, func in [("out", out), ("latch", latch), ("reset", reset)]:
        for target_label, make_target in COIL_TARGETS:
            trigger = a.bool()
            with Rung(trigger) as r:
                r.comment = f"{func_name}__{target_label}"
                func(make_target(a))

    # out with oneshot
    trigger = a.bool()
    with Rung(trigger) as r:
        r.comment = "out__oneshot"
        out(a.bool(), oneshot=True)

    # ── 3. Copy variants ────────────────────────────────────────────

    for label, make in COPY_VARIANTS:
        trigger = a.bool()
        src, dst = make(a)
        with Rung(trigger) as r:
            r.comment = f"copy__{label}"
            copy(src, dst)

    # copy with oneshot
    trigger = a.bool()
    with Rung(trigger) as r:
        r.comment = "copy__oneshot"
        copy(a.int_(), a.int_(), oneshot=True)

    # ── 4. Blockcopy variants ───────────────────────────────────────

    for label, make in BLOCKCOPY_VARIANTS:
        trigger = a.bool()
        src, dst = make(a)
        with Rung(trigger) as r:
            r.comment = f"blockcopy__{label}"
            blockcopy(src, dst)

    # blockcopy with oneshot
    trigger = a.bool()
    with Rung(trigger) as r:
        r.comment = "blockcopy__oneshot"
        blockcopy(a.int_block(4).select(1, 4), a.int_block(4).select(1, 4), oneshot=True)

    # ── 5. Fill variants ────────────────────────────────────────────

    for label, make in FILL_VARIANTS:
        trigger = a.bool()
        val, dst = make(a)
        with Rung(trigger) as r:
            r.comment = f"fill__{label}"
            fill(val, dst)

    # fill with oneshot
    trigger = a.bool()
    with Rung(trigger) as r:
        r.comment = "fill__oneshot"
        fill(0, a.int_block(4).select(1, 4), oneshot=True)

    # ── 6. Calc variants ────────────────────────────────────────────

    for label, make in CALC_VARIANTS:
        trigger = a.bool()
        expr, dest, os = make(a)
        with Rung(trigger) as r:
            r.comment = f"calc__{label}"
            calc(expr, dest, oneshot=os)

    # ── 7. Timer variants ───────────────────────────────────────────

    for label, make in TIMER_VARIANTS:
        trigger = a.bool()
        kind, (done, acc), preset, rst = make(a)
        fn = on_delay if kind == "on" else off_delay
        with Rung(trigger) as r:
            r.comment = f"{'on' if kind == 'on' else 'off'}_delay__{label}"
            builder = fn(done, acc, preset=preset, unit=Tms)
            if rst is not None:
                builder.reset(rst)

    # ── 8. Counter variants ─────────────────────────────────────────

    for label, make in COUNTER_VARIANTS:
        trigger = a.bool()
        kind, (done, acc), preset, dwn, rst = make(a)
        fn = count_up if kind == "up" else count_down
        with Rung(rise(trigger)) as r:
            r.comment = f"count_{label}"
            builder = fn(done, acc, preset=preset)
            if dwn is not None:
                builder = builder.down(dwn)
            builder.reset(rst)

    # ── 9. Search variants ──────────────────────────────────────────

    for label, op in SEARCH_OPS:
        trigger = a.bool()
        val, src_blk = a.int_(), a.int_block(10)
        result, found = a.int_(), a.bool()
        with Rung(trigger) as r:
            r.comment = f"search__{label}"
            search(op, val, src_blk.select(1, 10), result=result, found=found)

    # search with continuous
    trigger = a.bool()
    val, src_blk = a.int_(), a.int_block(10)
    result, found = a.int_(), a.bool()
    with Rung(trigger) as r:
        r.comment = "search__continuous"
        search("==", val, src_blk.select(1, 10), result=result, found=found, continuous=True)

    # search with oneshot
    trigger = a.bool()
    val, src_blk = a.int_(), a.int_block(10)
    result, found = a.int_(), a.bool()
    with Rung(trigger) as r:
        r.comment = "search__oneshot"
        search("==", val, src_blk.select(1, 10), result=result, found=found, oneshot=True)

    # ── 10. Shift register ──────────────────────────────────────────

    trigger = a.bool()
    clk, rst = a.bool(), a.bool()
    bits = a.bool_block(8)
    with Rung(trigger) as r:
        r.comment = "shift__basic"
        shift(bits.select(1, 8)).clock(clk).reset(rst)

    # ── 11. Pack / unpack ───────────────────────────────────────────

    trigger = a.bool()
    bits, dest = a.bool_block(16), a.int_()
    with Rung(trigger) as r:
        r.comment = "pack_bits"
        pack_bits(bits.select(1, 16), dest)

    trigger = a.bool()
    words, dest = a.int_block(2), a.dint()
    with Rung(trigger) as r:
        r.comment = "pack_words"
        pack_words(words.select(1, 2), dest)

    trigger = a.bool()
    chars, dest = a.char_block(4), a.int_()
    with Rung(trigger) as r:
        r.comment = "pack_text"
        pack_text(chars.select(1, 4), dest)

    trigger = a.bool()
    chars, dest = a.char_block(4), a.int_()
    with Rung(trigger) as r:
        r.comment = "pack_text__allow_whitespace"
        pack_text(chars.select(1, 4), dest, allow_whitespace=True)

    trigger = a.bool()
    src, bits = a.int_(), a.bool_block(16)
    with Rung(trigger) as r:
        r.comment = "unpack_to_bits"
        unpack_to_bits(src, bits.select(1, 16))

    trigger = a.bool()
    src, words = a.dint(), a.int_block(2)
    with Rung(trigger) as r:
        r.comment = "unpack_to_words"
        unpack_to_words(src, words.select(1, 2))

    # ── 12. Drums ───────────────────────────────────────────────────

    # Event drum — basic (reset only)
    trigger = a.bool()
    outs_tags = [a.bool(), a.bool()]
    evts = [a.bool(), a.bool()]
    step, flag, rst = a.int_(), a.bool(), a.bool()
    with Rung(trigger) as r:
        r.comment = "event_drum__basic"
        event_drum(
            outputs=outs_tags,
            events=evts,
            pattern=[[True, False], [False, True]],
            current_step=step,
            completion_flag=flag,
        ).reset(rst)

    # Event drum — full (reset + jump + jog)
    trigger = a.bool()
    outs_tags = [a.bool(), a.bool()]
    evts = [a.bool(), a.bool()]
    step, flag = a.int_(), a.bool()
    rst, jmp, jog_c = a.bool(), a.bool(), a.bool()
    with Rung(trigger) as r:
        r.comment = "event_drum__full"
        event_drum(
            outputs=outs_tags,
            events=evts,
            pattern=[[True, False], [False, True]],
            current_step=step,
            completion_flag=flag,
        ).reset(rst).jump(jmp, step=1).jog(jog_c)

    # Time drum — basic (reset only)
    trigger = a.bool()
    outs_tags = [a.bool(), a.bool()]
    step, flag, rst = a.int_(), a.bool(), a.bool()
    _, tmr_acc = a.timer()  # accumulator must be on TD bank
    with Rung(trigger) as r:
        r.comment = "time_drum__basic"
        time_drum(
            outputs=outs_tags,
            presets=[1000, 2000],
            unit=Tms,
            pattern=[[True, False], [False, True]],
            current_step=step,
            accumulator=tmr_acc,
            completion_flag=flag,
        ).reset(rst)

    # Time drum — full (reset + jump + jog)
    trigger = a.bool()
    outs_tags = [a.bool(), a.bool()]
    step, flag = a.int_(), a.bool()
    _, tmr_acc = a.timer()  # accumulator must be on TD bank
    rst, jmp, jog_c = a.bool(), a.bool(), a.bool()
    with Rung(trigger) as r:
        r.comment = "time_drum__full"
        time_drum(
            outputs=outs_tags,
            presets=[1000, 2000],
            unit=Tms,
            pattern=[[True, False], [False, True]],
            current_step=step,
            accumulator=tmr_acc,
            completion_flag=flag,
        ).reset(rst).jump(jmp, step=1).jog(jog_c)

    # ── 13. Forloop variants ────────────────────────────────────────

    for label, make in FORLOOP_VARIANTS:
        trigger = a.bool()
        cnt, os = make(a)
        src, dst = a.int_(), a.int_()
        with Rung(trigger) as r:
            r.comment = f"forloop__{label}"
            with forloop(cnt, oneshot=os):
                copy(src, dst)

    # ── 14. Send / receive (Modbus TCP) ─────────────────────────────

    trigger = a.bool()
    src = a.int_block(4)
    sending, success, error, exc = a.bool(), a.bool(), a.bool(), a.int_()
    with Rung(trigger) as r:
        r.comment = "send__basic"
        send(
            target=ModbusTarget(name="plc2", ip="192.168.1.10"),
            remote_start="DS1",
            source=src.select(1, 4),
            sending=sending,
            success=success,
            error=error,
            exception_response=exc,
        )

    trigger = a.bool()
    dst = a.int_block(4)
    receiving, success, error, exc = a.bool(), a.bool(), a.bool(), a.int_()
    with Rung(trigger) as r:
        r.comment = "receive__basic"
        receive(
            target=ModbusTarget(name="plc2", ip="192.168.1.10"),
            remote_start="DS1",
            dest=dst.select(1, 4),
            receiving=receiving,
            success=success,
            error=error,
            exception_response=exc,
        )


# ── Build TagMap and export ─────────────────────────────────────────

mapping = a.tagmap()

if __name__ == "__main__":
    out_dir = Path("fixtures/coverage")
    bundle = mapping.to_ladder(coverage_program)
    bundle.write(out_dir)
    print(f"Wrote {len(coverage_program.rungs)} rungs to {out_dir}/main.csv")
