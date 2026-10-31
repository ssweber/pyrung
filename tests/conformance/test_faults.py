"""Which stores raise a fault flag: the ``faults`` group (subroutine ``Conf_Faults``).

The fault flags a CLICK raises on a store: SC40 (division error), SC43 (out
of range), SC44 (address error) and SC46 (Math operation error), for a Math
whose result does not fit its destination, a Copy that clamps, RAND past an
Int, a pointer past its bank, and Inf and NaN in a Real.  Part G of the
CLICK project (``suite.PARTS``), so these probes reload on their own.

Each probe runs one instruction (its *op*) once and reads one flag on the
rung right after it.  A flag that is on shows only that some instruction set
it: on a CLICK SC40 follows the last Math and SC43 and SC44 the last
instruction (hardware round 2), while pyrung keeps every flag on to the end
of the scan, and over the wire the cases start on different scans.  So a
probe runs its op on the first scan from 3 on which the flag it reads is off
just before the op (:func:`_probe`): a bit (``now``) comes on on that scan
only, and another (``taken``) latches so the op runs once.  An op whose flag
an earlier op of the same scan left on waits for the next scan.  On a
CLICK SC43 and SC44 follow the last instruction and SC40 the last Math, so
they are mostly off already (each op starts with a Copy), but SC46 stays on
to the end of the scan once a Math error raises it: there the gate is what
makes a probe of SC46 read its own op.  Two flags after one op are two
probes, each running the op again: on a CLICK the Math that records the
first would clear the second.
Inf and NaN are staged on scan 2 (``_stage_inf``), before any probe.
Every case has a 40-scan window, more than every probe of the group needs
when each waits for the one before (pyrung, in-process).

The registers are the group's own (``memory.OWNED``): sixteen C bits per
case from C201, staged DD501..DD600, DF451..DF495 and TXT401..TXT500.  A
flag bit is 1 when the flag is on right after the op; a destination a model
could leave alone is preset (99, or 1.0 for a Real) on the rung before.

The expectations are what a CLICK does where it has answered, and the
adopted answer of an open probe otherwise.  CLICK's help names the stores
that raise SC43: a Copy (Single, Block, Pack) that clamps, a text->number
conversion that fails, a Modbus Send/Receive Slave ID out of range, a Send
ASCII numeric out of range; it never names SC43 for a Math.  What each case
tells apart:

- ``math_store_out_of_range`` (probe): which Math results raise SC43, each
  op on its own.  (a) 2147483647 + 1 into a DD wraps to -2147483648; (b)
  39999 + 1 from a DD into an Int, 40000, which wraps to -25536 (Result4);
  (c) 2147483647 + 2147483647 + 7 into an Int: 4294967301, which wraps
  through 32 bits to 5 (ErrorCode) and fits.  Result1..3 are SC43 after (a),
  (b) and (c).  Adopted (the hypothesis from the help and the 2026-10-31
  reading of the since retired ``system/out_of_range_flag_vs_other_instructions``,
  where a Dint wrap raised nothing): a Math raises SC43 only when its result
  does not fit a destination narrower than 32 bits, judged on the whole
  result: 0, 1, 1.
  Judged on the result wrapped to 32 bits: 0, 1, 0.  Every wrap raises it
  (pyrung, step SC43): 1, 1, 1.
- ``copy_clamp_out_of_range`` (probe): a Copy that clamps, 40000 and -40000
  from a DD and 1.0E+10 from a DF into an Int: 32767, -32768, 32767
  (Result1..3; ``numeric/copy_clamps_calc_wraps`` read the clamp), and SC43
  after each as bits 1, 2 and 4 of Result4.  Adopted (the help): 7.  pyrung
  (step SC43) clamps without the flag: 0.
- ``rand_wide_bounds_flags`` (probe): the flags of
  ``numeric/int_math_rand_wide_bounds``, which a CLICK read as 0 (2026-10-31).
  RAND ( 70000 , 70000 ) / 2 into an Int preset 99 (Result4), and SC40, SC43
  and SC46 after it as bits 1, 2 and 4 of Result1.  The control RAND ( 7 , 7
  ) into Result3, its flags in Result2.  Adopted: a Math error, SC46 only,
  as a Math error writes 0 (``math_error``): 4, 0, 7, 0.  A store refused as
  out of range: 2.  No flag: 0.  pyrung (step MATH) wraps 35000 to -30536
  with SC43: 2, 0, 7, -30536.
- ``rand_real_bounds``: moved to the ``paste_risk`` group (part C), since
  a CLICK may refuse a DF bound at paste; its C bits (C249..C264) and
  DF461..DF462 stay unused here.
- ``pointer_past_bank`` (probe): a Copy through a pointer past the DD bank
  (1001, Result1, preset 99) and below it (-1, Result3), SC43 and SC44 after
  each as bits 1 and 2 (Result2, ErrorCode).  ``system/address_flag...`` asks
  it of pointer 0.  Adopted (pyrung): an address error, the destination left
  alone: 99, 2, 99, 2.  A pointer that wraps or clamps into the bank writes
  a DD's 0.
- ``power_overflow_is_inf`` (probe): 10.0 ^ 100 and -10.0 ^ 101 into a Real
  preset 1.0, past a float32.  Adopted, as ``numeric/real_overflow...``
  read for a product: +Inf (half of it still past 3.0E+38, Result1 = 1) and
  -Inf (Result3 = 1), SC43 on after each (Result2, Result4).  A ^ that
  saturates reads Result1 = 0; one that is a Math error writes 0 (Result1 =
  0, Result3 = 0).
- ``real_intermediate_range`` (probe): 3.0E+38 * 10 / 100 into a Real.  An
  intermediate wider than a float32 (pyrung, adopted) holds 3.0E+39 and
  stores 3.0E+37 (Result1 = 1, Result2 = 0, Result3 = 0).  A float32
  intermediate overflows to +Inf, and Inf / 100 is +Inf (Result1 = 0, Result2
  = 1, SC43 Result3 = 1).  (``numeric/real_is_64_bit_in_pyrung`` is the
  precision of a stored Real, in-process only.)
- ``inf_and_nan_arithmetic`` (probe): staged +Inf (3.0E+38 * 10, scan 2) * 2
  is +Inf (Result1 = 1) with SC43 on (Result2).  Inf - Inf into a Real is
  NaN: Result3 has bit 1 for NaN = NaN, 2 for NaN > 0, 4 for NaN < 0 and 8
  for NaN <> NaN; IEEE compares read 8.  SC43 and SC46 after Inf - Inf are
  bits 1 and 2 of Result4.  Adopted (pyrung): 1, 1, 8, 1.  A CLICK whose
  compares are not IEEE reads Result3 as something else; one that turns a
  non-finite operand into a Math error writes 0 (Result1 = 0; the 0 that
  stands for NaN compares equal to itself, Result3 = 1) with SC46.
- ``non_finite_math_into_int`` (probe): a Math of +Inf into an Int preset 99
  (Result1), of NaN into an Int (Result2), of +Inf into a DD preset 99
  (Result3: 1 for 0, 2 for 99, 3 for 2147483647, 4 for -2147483648, 5 for
  anything else), and SC43 after each as bits 1, 2 and 4 of Result4.
  Adopted (pyrung): an out-of-range store that writes 0: 0, 0, 1, 7.  A
  saturating store reads 32767 and 3.  A skipped write reads 99 and 2.
- ``copy_non_finite_to_int`` (probe): a Copy of +Inf, -Inf and NaN into an
  Int preset 99 (Result1..3), SC43 after each as bits 1, 2 and 4 of Result4.
  The help says a Copy that clamps raises SC43; adopted: +Inf and -Inf clamp
  (32767, -32768) and NaN writes 0, each with SC43: 7.  pyrung (step SC43)
  converts the same without the flag: 32767, -32768, 0, 0.
- ``copy_inf_to_text`` (probe): +Inf copied into TXT401 as text (Copy
  Option 4c), TXT401 seeded ``Z``: Result1 is TXT401's code, Result2 SC43.
  Adopted (pyrung): the conversion fails, the text is left alone, SC43 on:
  90, 1.  A CLICK that writes ``INF`` (or ``+INF``) reads 73 (or 43), with
  or without the flag.
- ``non_finite_functions`` (probe): SIN ( +Inf ) and SQRT ( -Inf ) into a
  Real preset 1.0: Result1 = 1 if SIN wrote 0, Result2 SC46 and Result3 SC43
  after it; Result4 = 1 if SQRT wrote 0, ErrorCode SC46 after it.  Adopted
  (as ``math_error`` reads SQRT ( -4.0 )): Math errors that write 0: 1, 1,
  0, 1, 1.  An IEEE function stores NaN (Result1 = 0, Result4 = 0).
  pyrung (step MATH) writes no 0 from SIN and raises SC43, and writes 0 from SQRT
  with SC46: 0, 0, 1, 1, 1.

Not asked here, because the conformance harness cannot: a Modbus Send or
Receive whose Slave ID is out of range needs a second Modbus device on the
CLICK's port; how a DF holding +Inf or NaN reads over Modbus (the harness
reads slot DS only) or on the CLICK's display.  A Math with a pointer is not
expressible: a CLICK has pointers in a Single Copy only.  FOR with a
non-finite count is ``for_count``, part D, since a CLICK that loops for ever
stops on its watchdog.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from pyrung.conformance import Group, assert_all_passed, case, run
from pyrung.core import Rung, calc, copy, fill, latch, out, reset, system, to_ascii, to_text
from pyrung.core.expression import randint, sin, sqrt
from tests.conformance.memory import c, dd, df, txt
from tests.conformance.scans import count_scans, done, scans_for

#: Scans a case runs: every probe of the group, one per scan, and a margin.
WINDOW = 40
SCANS = scans_for(WINDOW)
#: The scan Inf and NaN are staged on; probes start on the next.
STAGE_SCAN = 2

SC40 = system.fault.division_error
SC43 = system.fault.out_of_range
SC44 = system.fault.address_error
SC46 = system.fault.math_operation_error
NUL = "\x00"

#: Sixteen C bits per case, from C201, in the order of the cases below.
_BITS_PER_CASE = 16


def _bits(index: int) -> list[Any]:
    first = 201 + _BITS_PER_CASE * index
    return [c[n] for n in range(first, first + _BITS_PER_CASE)]


class _Probes:
    """Hands out a case's (now, taken) bit pairs, in order."""

    def __init__(self, index: int) -> None:
        self.free = iter(_bits(index))

    def __call__(
        self, slot: Any, op: Callable[[], None], flag: Any, field: Any, bit: int = 1
    ) -> None:
        _probe(slot, next(self.free), next(self.free), op, flag, field, bit)


def _probe(slot, now, taken, op, flag, field, bit):
    """Run *op* once, on the first scan from 3 with *flag* off right before it;
    add *bit* to *field* if *flag* is on right after it.
    """
    with Rung(slot.Cmd != 0, slot.Scratch > STAGE_SCAN, slot.Scratch <= WINDOW, ~taken, ~flag):
        out(now)
        latch(taken)
    with Rung(now):
        op()
    with Rung(now, flag):
        calc(field + bit, field)


def _stage(slot, *instructions):
    """Run *instructions* on scan 2, before every probe."""
    with Rung(slot.Cmd != 0, slot.Scratch == STAGE_SCAN):
        for instruction in instructions:
            instruction()


def _end(slot):
    """The scan that raises ``Fired``: read the destinations back, once."""
    return (slot.Cmd != 0, slot.Scratch > WINDOW, slot.Fired == 0)


def _stage_inf(into):
    return [lambda: copy(3.0e38, into), lambda: calc(into * 10, into)]


def _reset_bits(index):
    for bit in _bits(index):
        reset(bit)


def _init(index, dds=(), dfs=(), txts=()):
    def init(slot):
        _reset_bits(index)
        for first, last in dds:
            fill(0, dd.select(first, last))
        for first, last in dfs:
            fill(0.0, df.select(first, last))
        for first, last in txts:
            fill(NUL, txt.select(first, last))

    return init


def prove_math_store_out_of_range(slot):
    probe = _Probes(0)
    count_scans(slot, WINDOW)
    _stage(slot, lambda: copy(2147483647, dd[501]), lambda: copy(39999, dd[503]))

    def dint_wrap():
        copy(0, dd[502])
        calc(dd[501] + 1, dd[502])

    def into_int():
        copy(99, slot.Result4)
        calc(dd[503] + 1, slot.Result4)

    def wrap_then_fits():
        copy(99, slot.ErrorCode)
        calc(dd[501] + dd[501] + 7, slot.ErrorCode)

    probe(slot, dint_wrap, SC43, slot.Result1)
    probe(slot, into_int, SC43, slot.Result2)
    probe(slot, wrap_then_fits, SC43, slot.Result3)
    done(slot, WINDOW)


def prove_copy_clamp_out_of_range(slot):
    probe = _Probes(1)
    count_scans(slot, WINDOW)
    _stage(
        slot,
        lambda: copy(40000, dd[511]),
        lambda: copy(-40000, dd[512]),
        lambda: copy(1.0e10, df[451]),
    )
    sources = (dd[511], dd[512], df[451])
    fields = (slot.Result1, slot.Result2, slot.Result3)
    for k, (source, field) in enumerate(zip(sources, fields, strict=True)):

        def op(source=source, field=field):
            copy(99, field)
            copy(source, field)

        probe(slot, op, SC43, slot.Result4, 1 << k)
    done(slot, WINDOW)


def prove_rand_wide_bounds_flags(slot):
    probe = _Probes(2)
    count_scans(slot, WINDOW)

    def wide():
        copy(99, slot.Result4)
        calc(randint(70000, 70000) / 2, slot.Result4)

    def control():
        copy(99, slot.Result3)
        calc(randint(7, 7), slot.Result3)

    for k, flag in enumerate((SC40, SC43, SC46)):
        probe(slot, wide, flag, slot.Result1, 1 << k)
    for k, flag in enumerate((SC40, SC43, SC46)):
        probe(slot, control, flag, slot.Result2, 1 << k)
    done(slot, WINDOW)


def prove_pointer_past_bank(slot):
    probe = _Probes(4)
    count_scans(slot, WINDOW)
    for pointer, dest, flags in (
        (1001, slot.Result1, slot.Result2),
        (-1, slot.Result3, slot.ErrorCode),
    ):

        def op(pointer=pointer, dest=dest):
            copy(pointer, slot.Result4)
            copy(99, dest)
            copy(dd[slot.Result4], dest)

        for k, flag in enumerate((SC43, SC44)):
            probe(slot, op, flag, flags, 1 << k)
    done(slot, WINDOW)


def prove_power_overflow_is_inf(slot):
    probe = _Probes(5)
    count_scans(slot, WINDOW)
    _stage(slot, lambda: copy(10.0, df[471]), lambda: copy(-10.0, df[472]))

    def positive():
        copy(1.0, df[473])
        calc(df[471] ** 100, df[473])

    def negative():
        copy(1.0, df[474])
        calc(df[472] ** 101, df[474])

    probe(slot, positive, SC43, slot.Result2)
    probe(slot, negative, SC43, slot.Result4)
    with Rung(*_end(slot)):
        calc(df[473] / 2, df[475])
        calc(df[474] / 2, df[476])
    with Rung(*_end(slot), df[475] >= 3.0e38):
        copy(1, slot.Result1)
    with Rung(*_end(slot), df[476] <= -3.0e38):
        copy(1, slot.Result3)
    done(slot, WINDOW)


def prove_real_intermediate_range(slot):
    probe = _Probes(6)
    count_scans(slot, WINDOW)
    _stage(slot, lambda: copy(3.0e38, df[481]))

    def op():
        copy(1.0, df[482])
        calc(df[481] * 10 / 100, df[482])

    probe(slot, op, SC43, slot.Result3)
    with Rung(*_end(slot), df[482] >= 2.9e37, df[482] <= 3.1e37):
        copy(1, slot.Result1)
    with Rung(*_end(slot), df[482] >= 3.0e38):
        copy(1, slot.Result2)
    done(slot, WINDOW)


def prove_inf_and_nan_arithmetic(slot):
    probe = _Probes(7)
    count_scans(slot, WINDOW)
    inf, doubled, nan = df[483], df[484], df[485]
    _stage(slot, *_stage_inf(inf))

    def double():
        copy(1.0, doubled)
        calc(inf * 2, doubled)

    def not_a_number():
        copy(1.0, nan)
        calc(inf - inf, nan)

    probe(slot, double, SC43, slot.Result2)
    for k, flag in enumerate((SC43, SC46)):
        probe(slot, not_a_number, flag, slot.Result4, 1 << k)
    end = _end(slot)
    with Rung(*end, doubled >= 3.0e38):
        copy(1, slot.Result1)
    for bit, compare in ((1, nan == nan), (2, nan > 0), (4, nan < 0), (8, nan != nan)):
        with Rung(*end, compare):
            calc(slot.Result3 + bit, slot.Result3)
    done(slot, WINDOW)


def prove_non_finite_math_into_int(slot):
    probe = _Probes(8)
    count_scans(slot, WINDOW)
    inf, nan = df[486], df[487]
    _stage(slot, *_stage_inf(inf), lambda: calc(inf - inf, nan))

    def inf_int():
        copy(99, slot.Result1)
        calc(inf, slot.Result1)

    def nan_int():
        copy(99, slot.Result2)
        calc(nan, slot.Result2)

    def inf_dint():
        copy(99, dd[521])
        calc(inf, dd[521])

    for k, op in enumerate((inf_int, nan_int, inf_dint)):
        probe(slot, op, SC43, slot.Result4, 1 << k)
    end = _end(slot)
    with Rung(*end):
        copy(5, slot.Result3)
    for value, reading in ((0, 1), (99, 2), (2147483647, 3), (-2147483648, 4)):
        with Rung(*end, dd[521] == value):
            copy(reading, slot.Result3)
    done(slot, WINDOW)


def prove_copy_non_finite_to_int(slot):
    probe = _Probes(9)
    count_scans(slot, WINDOW)
    inf, minus_inf, nan = df[488], df[489], df[490]
    _stage(
        slot,
        *_stage_inf(inf),
        lambda: calc(inf * -1, minus_inf),
        lambda: calc(inf - inf, nan),
    )
    fields = (slot.Result1, slot.Result2, slot.Result3)
    for k, (source, field) in enumerate(zip((inf, minus_inf, nan), fields, strict=True)):

        def op(source=source, field=field):
            copy(99, field)
            copy(source, field)

        probe(slot, op, SC43, slot.Result4, 1 << k)
    done(slot, WINDOW)


def prove_copy_inf_to_text(slot):
    probe = _Probes(10)
    count_scans(slot, WINDOW)
    inf = df[491]
    _stage(slot, *_stage_inf(inf))

    def op():
        copy("Z", txt[401])
        copy(inf, txt[401], convert=to_text())

    probe(slot, op, SC43, slot.Result2)
    with Rung(*_end(slot)):
        copy(txt[401], slot.Result1, convert=to_ascii)
    done(slot, WINDOW)


def prove_non_finite_functions(slot):
    probe = _Probes(11)
    count_scans(slot, WINDOW)
    inf, minus_inf, sine, root = df[492], df[493], df[494], df[495]
    _stage(slot, *_stage_inf(inf), lambda: calc(inf * -1, minus_inf))

    def sine_op():
        copy(1.0, sine)
        calc(sin(inf), sine)

    def root_op():
        copy(1.0, root)
        calc(sqrt(minus_inf), root)

    probe(slot, sine_op, SC46, slot.Result2)
    probe(slot, sine_op, SC43, slot.Result3)
    probe(slot, root_op, SC46, slot.ErrorCode)
    end = _end(slot)
    with Rung(*end, sine == 0):
        copy(1, slot.Result1)
    with Rung(*end, root == 0):
        copy(1, slot.Result4)
    done(slot, WINDOW)


group = Group(
    "faults",
    [
        case(
            "A Math raises the out-of-range flag SC43 only when its result does not fit a "
            "destination narrower than 32 bits: 2147483647 + 1 into a DD wraps without it "
            "(Result1 = 0); 39999 + 1 into an Int raises it (Result2 = 1) and stores "
            "-25536 (Result4); 2147483647 + 2147483647 + 7 into an Int raises it (Result3 "
            "= 1) and stores 5 (ErrorCode)",
            ladder=prove_math_store_out_of_range,
            init=_init(0, dds=[(501, 503)]),
            expect={"Result1": 0, "Result2": 1, "Result3": 1, "Result4": -25536, "ErrorCode": 5},
            scans=SCANS,
            pending="SC43",
            probe="which Math stores raise SC43: Dint wrap, Int overflow, 32-bit wrap that fits",
        ),
        case(
            "A Copy that clamps raises the out-of-range flag SC43: 40000 and -40000 from a "
            "DD and 1.0E+10 from a DF into an Int store 32767, -32768 and 32767 (Result1..3) "
            "and each raises SC43 (Result4 = 7)",
            ladder=prove_copy_clamp_out_of_range,
            init=_init(1, dds=[(511, 512)], dfs=[(451, 451)]),
            expect={"Result1": 32767, "Result2": -32768, "Result3": 32767, "Result4": 7},
            scans=SCANS,
            pending="SC43",
            probe="does a clamping Copy raise SC43",
        ),
        case(
            "RAND with bounds past an Int is a Math error: RAND ( 70000 , 70000 ) / 2 into "
            "an Int writes 0 (Result4) and raises SC46 alone (Result1 = 4); RAND ( 7 , 7 ) "
            "writes 7 (Result3) and raises nothing (Result2 = 0)",
            ladder=prove_rand_wide_bounds_flags,
            init=_init(2),
            expect={"Result1": 4, "Result2": 0, "Result3": 7, "Result4": 0},
            scans=SCANS,
            pending="MATH",
            probe="which flag RAND past an Int raises",
        ),
        case(
            "A Copy through a pointer outside the DD bank is an address error that leaves "
            "its destination alone: pointer 1001 leaves 99 (Result1) and raises SC44, not "
            "SC43 (Result2 = 2); pointer -1 the same (Result3 = 99, ErrorCode = 2)",
            ladder=prove_pointer_past_bank,
            init=_init(4),
            expect={"Result1": 99, "Result2": 2, "Result3": 99, "ErrorCode": 2},
            scans=SCANS,
            probe="a pointer past or below its bank: which flag, is the destination written",
        ),
        case(
            "A power that overflows a Real stores +/-Inf with SC43: 10.0 ^ 100 is +Inf "
            "(half of it past 3.0E+38, Result1 = 1; SC43 Result2 = 1) and -10.0 ^ 101 is "
            "-Inf (Result3 = 1; SC43 Result4 = 1)",
            ladder=prove_power_overflow_is_inf,
            init=_init(5, dfs=[(471, 476)]),
            expect={"Result1": 1, "Result2": 1, "Result3": 1, "Result4": 1},
            scans=SCANS,
            probe="what ^ does past a float32",
        ),
        case(
            "A Real Math's intermediate is wider than a float32: 3.0E+38 * 10 / 100 stores "
            "3.0E+37 (Result1 = 1), not +Inf (Result2 = 0), and raises no SC43 (Result3 = 0)",
            ladder=prove_real_intermediate_range,
            init=_init(6, dfs=[(481, 482)]),
            expect={"Result1": 1, "Result2": 0, "Result3": 0},
            scans=SCANS,
            probe="float32 or wider Real intermediates",
        ),
        case(
            "Inf and NaN follow IEEE in a Real Math: +Inf * 2 is +Inf (Result1 = 1) with "
            "SC43 (Result2 = 1); +Inf - +Inf is NaN, which equals nothing and compares "
            "neither above nor below 0, only <> (Result3 = 8), with SC43 and no SC46 "
            "(Result4 = 1)",
            ladder=prove_inf_and_nan_arithmetic,
            init=_init(7, dfs=[(483, 485)]),
            expect={"Result1": 1, "Result2": 1, "Result3": 8, "Result4": 1},
            scans=SCANS,
            probe="Inf and NaN in a Real Math: propagation, flags, compares",
        ),
        case(
            "A Math of +Inf or NaN into an integer writes 0 with SC43: +Inf into an Int "
            "(Result1 = 0), NaN into an Int (Result2 = 0) and +Inf into a DD (Result3 = 1, "
            "0), each preset 99, all with SC43 (Result4 = 7)",
            ladder=prove_non_finite_math_into_int,
            init=_init(8, dds=[(521, 521)], dfs=[(486, 487)]),
            expect={"Result1": 0, "Result2": 0, "Result3": 1, "Result4": 7},
            scans=SCANS,
            probe="Math of Inf or NaN into an Int or a DD",
        ),
        case(
            "A Copy of +Inf, -Inf or NaN into an Int clamps and raises SC43: +Inf stores "
            "32767 (Result1), -Inf -32768 (Result2), NaN 0 (Result3), each over a preset "
            "99 and each with SC43 (Result4 = 7)",
            ladder=prove_copy_non_finite_to_int,
            init=_init(9, dfs=[(488, 490)]),
            expect={"Result1": 32767, "Result2": -32768, "Result3": 0, "Result4": 7},
            scans=SCANS,
            probe="Copy of Inf or NaN into an Int",
            pending="SC43",
        ),
        case(
            "A Copy of +Inf to text fails: TXT401 keeps its Z (Result1 = 90) and SC43 is "
            "on (Result2 = 1)",
            ladder=prove_copy_inf_to_text,
            init=_init(10, dfs=[(491, 491)], txts=[(401, 415)]),
            expect={"Result1": 90, "Result2": 1},
            scans=SCANS,
            probe="Copy of +Inf to text",
        ),
        case(
            "SIN ( +Inf ) and SQRT ( -Inf ) are Math errors: each writes 0 over a Real's "
            "1.0 (Result1 = 1, Result4 = 1) with SC46 (Result2 = 1, ErrorCode = 1), and SIN "
            "raises no SC43 (Result3 = 0)",
            ladder=prove_non_finite_functions,
            init=_init(11, dfs=[(492, 495)]),
            expect={"Result1": 1, "Result2": 1, "Result3": 0, "Result4": 1, "ErrorCode": 1},
            scans=SCANS,
            probe="SIN and SQRT of an infinity",
            pending="MATH",
        ),
    ],
)


def test_faults():
    assert_all_passed(run(group))
