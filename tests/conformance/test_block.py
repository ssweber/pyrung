"""Block instructions: the ``block`` group (subroutine ``Conf_Block``).

``blockcopy``, ``fill``, ``pack_bits``/``unpack_to_bits`` and
``pack_words``/``unpack_to_words``.  A slot's fields are not a range, so each
case stages its sources in raw memory it owns (``memory.OWNED``): DS4001..DS4110
(ten per case), C1201.., DD201.., DF201.. -- and reads the result back into its
slot.  Its ``init`` puts the staged registers and bits back to zero.  A DD or
Real result is read back through a ``calc`` that subtracts a constant, so
a result that does not fit an Int still lands in one.

Static facts, no case: ``blockcopy`` takes only the ``to_value`` and
``to_ascii`` converters (the others raise ``ValueError`` when the instruction
is built), and a source and destination of different length raise when the
instruction runs, not at build time.

The expectations are pyrung's answers, unconfirmed on a CLICK.  The cases
that tell pyrung from a different CLICK model:

- ``blockcopy_range``: element ``i`` of the source goes to element ``i`` of
  the destination, and nothing past the destination is written.  A reversed
  copy would read 44 in ``Result2``.
- ``blockcopy_overlap``: the whole source is read before the destination is
  written, so DS{1,2,3} copied onto DS{2,3,4} gives {1,1,2,3}.  A copy that
  writes as it reads gives {1,1,1,1}: ``Result3`` and ``Result4`` read 1.
- ``blockcopy_types``: a block copy converts element by element like
  ``copy``: -2 DS -> DD stays -2, 70000 DD -> DS clamps to 32767.  A raw
  word-for-word copy, or a wrapping copy, reads something else (70000 wraps
  to 4464).
- ``fill_constant``: a fill writes the value to the whole range and not past it.
- ``every_scan``: ``blockcopy``, ``fill`` and ``copy`` run on every scan their
  rung is true.  Overwritten between scans, the destination is written again
  on the next scan (``Result1..3``); an edge-triggered instruction leaves
  99 there.  ``Result4`` shows the overwrite did survive until the scan
  that restored it.
- ``pack_bits_int``: ``C1201`` is bit 0 (LSB first), the whole destination is
  written (junk above the packed bits is cleared), and bit 15 makes an Int
  negative.  MSB-first packing reads 208 in ``Result1``; an OR-in leaves the
  junk (32523).
- ``pack_bits_real``: a REAL destination holds the raw IEEE-754 bits, not
  the integer value: bits 21 and 30 are 2.5 (0x40200000, high word 0x4020).
  A numeric conversion would give 1075838976.0.
- ``unpack_bits``: bit 0 goes to the first bit of the range, and bit 15 of an
  Int is its sign bit.
- ``pack_words``: the first source is the low word, the low word is not sign
  extended (-1 and 1 pack to 131071; a sign-extending add gives 65535), and
  the same bits into a REAL are 1.0.
- ``unpack_words``: low word first; a low word with the top bit set lands in
  an Int as a negative (wrapped, not clamped to 32767).
"""

from __future__ import annotations

from pyrung.conformance import Group, assert_all_passed, case, run
from pyrung.core import (
    Rung,
    blockcopy,
    calc,
    copy,
    fill,
    latch,
    pack_bits,
    pack_words,
    reset,
    unpack_to_bits,
    unpack_to_words,
)
from tests.conformance.memory import c, dd, df, ds
from tests.conformance.scans import count_scans, done, scans_for

SCANS = scans_for()


def _go(slot):
    """The rung condition of a case that fires on its first scan."""
    return (slot.Cmd != 0, slot.Fired == 0)


def _fire(slot):
    with Rung(*_go(slot)):
        copy(1, slot.Fired)


# --- blockcopy -------------------------------------------------------------


def prove_blockcopy_range(slot):
    with Rung(*_go(slot)):
        copy(11, ds[4001])
        copy(22, ds[4002])
        copy(33, ds[4003])
        copy(44, ds[4004])
        copy(5, ds[4015])
    with Rung(*_go(slot)):
        blockcopy(ds.select(4001, 4004), ds.select(4011, 4014))
    with Rung(*_go(slot)):
        copy(ds[4011], slot.Result1)
        copy(ds[4012], slot.Result2)
        copy(ds[4014], slot.Result3)
        copy(ds[4015], slot.Result4)
    _fire(slot)


def init_blockcopy_range(slot):
    fill(0, ds.select(4001, 4015))


def prove_blockcopy_overlap(slot):
    # Source 4101..4103 onto 4102..4104.
    with Rung(*_go(slot)):
        copy(1, ds[4101])
        copy(2, ds[4102])
        copy(3, ds[4103])
        copy(4, ds[4104])
    with Rung(*_go(slot)):
        blockcopy(ds.select(4101, 4103), ds.select(4102, 4104))
    with Rung(*_go(slot)):
        copy(ds[4101], slot.Result1)
        copy(ds[4102], slot.Result2)
        copy(ds[4103], slot.Result3)
        copy(ds[4104], slot.Result4)
    _fire(slot)


def init_blockcopy_overlap(slot):
    fill(0, ds.select(4101, 4110))


def prove_blockcopy_types(slot):
    # DS -> DD keeps the value; DD -> DS clamps like copy does.
    with Rung(*_go(slot)):
        copy(-2, ds[4031])
        copy(300, ds[4032])
        copy(70000, dd[203])
        copy(-70000, dd[204])
    with Rung(*_go(slot)):
        blockcopy(ds.select(4031, 4032), dd.select(201, 202))
        blockcopy(dd.select(203, 204), ds.select(4033, 4034))
    with Rung(*_go(slot)):
        copy(dd[201], slot.Result1)
        copy(dd[202], slot.Result2)
        copy(ds[4033], slot.Result3)
        copy(ds[4034], slot.Result4)
    _fire(slot)


def init_blockcopy_types(slot):
    fill(0, ds.select(4031, 4040))
    fill(0, dd.select(201, 204))


# --- fill ------------------------------------------------------------------


def prove_fill_constant(slot):
    # Fill 4021..4026; 4027 is the sentinel.
    with Rung(*_go(slot)):
        copy(1, ds[4027])
    with Rung(*_go(slot)):
        fill(7, ds.select(4021, 4026))
        fill(-3, dd.select(211, 213))
    with Rung(*_go(slot)):
        copy(ds[4021], slot.Result1)
        copy(ds[4026], slot.Result2)
        copy(ds[4027], slot.Result3)
        copy(dd[213], slot.Result4)
    _fire(slot)


def init_fill_constant(slot):
    fill(0, ds.select(4021, 4030))
    fill(0, dd.select(211, 213))


def prove_every_scan(slot):
    count_scans(slot)
    with Rung(slot.Cmd != 0):
        copy(11, ds[4091])
        copy(12, ds[4092])
    # Scan 4, before the instructions run: the scan 3 overwrite is still there.
    with Rung(slot.Cmd != 0, slot.Scratch == 4):
        copy(ds[4093], slot.Result4)
    with Rung(slot.Cmd != 0):
        blockcopy(ds.select(4091, 4092), ds.select(4093, 4094))
        fill(7, ds.select(4095, 4096))
        copy(5, ds[4097])
    with Rung(slot.Cmd != 0, slot.Scratch == 3):
        copy(99, ds[4093])
        copy(99, ds[4095])
        copy(99, ds[4097])
    with Rung(slot.Cmd != 0, slot.Scratch == 4):
        copy(ds[4093], slot.Result1)
        copy(ds[4095], slot.Result2)
        copy(ds[4097], slot.Result3)
    done(slot)


def init_every_scan(slot):
    fill(0, ds.select(4091, 4100))


# --- pack / unpack bits ----------------------------------------------------


def prove_pack_bits_int(slot):
    # C1201, C1202, C1204 and C1216 are on.
    with Rung(*_go(slot)):
        latch(c[1201])
        latch(c[1202])
        latch(c[1204])
        latch(c[1216])
        copy(0x7F00, ds[4042])
    with Rung(*_go(slot)):
        pack_bits(c.select(1201, 1208), ds[4042])
        pack_bits(c.select(1201, 1216), ds[4041])
    with Rung(*_go(slot)):
        copy(ds[4042], slot.Result1)
        copy(ds[4041], slot.Result2)
    _fire(slot)


def init_pack_bits_int(slot):
    for n in (1201, 1202, 1204, 1216):
        reset(c[n])
    fill(0, ds.select(4041, 4050))


def prove_pack_bits_real(slot):
    # Bit 21 is C1242 and bit 30 is C1251: 0x40200000 = 2.5.
    with Rung(*_go(slot)):
        latch(c[1242])
        latch(c[1251])
    with Rung(*_go(slot)):
        pack_bits(c.select(1221, 1252), df[201])
    with Rung(*_go(slot)):
        unpack_to_words(df[201], ds.select(4051, 4052))
    with Rung(*_go(slot)):
        copy(ds[4051], slot.Result1)
        copy(ds[4052], slot.Result2)
        calc(df[201] * 10, slot.Result3)
    _fire(slot)


def init_pack_bits_real(slot):
    reset(c[1242])
    reset(c[1251])
    copy(0, df[201])
    fill(0, ds.select(4051, 4060))


def prove_unpack_bits(slot):
    # 0x8005: bits 0, 2 and 15.
    with Rung(*_go(slot)):
        copy(-32763, ds[4061])
    with Rung(*_go(slot)):
        unpack_to_bits(ds[4061], c.select(1261, 1276))
    with Rung(*_go(slot), c[1261]):
        copy(1, slot.Result1)
    with Rung(*_go(slot), c[1262]):
        copy(1, slot.Result2)
    with Rung(*_go(slot), c[1263]):
        copy(1, slot.Result3)
    with Rung(*_go(slot), c[1276]):
        copy(1, slot.Result4)
    _fire(slot)


def init_unpack_bits(slot):
    for n in range(1261, 1277):
        reset(c[n])
    fill(0, ds.select(4061, 4070))


# --- pack / unpack words ---------------------------------------------------


def prove_pack_words(slot):
    with Rung(*_go(slot)):
        copy(1, ds[4073])
        copy(2, ds[4074])
        copy(-1, ds[4071])
        copy(1, ds[4072])
        copy(0, ds[4075])
        copy(-1, ds[4076])
        copy(0, ds[4077])
        copy(16256, ds[4078])
    with Rung(*_go(slot)):
        pack_words(ds.select(4073, 4074), dd[222])
        pack_words(ds.select(4071, 4072), dd[221])
        pack_words(ds.select(4075, 4076), dd[223])
        pack_words(ds.select(4077, 4078), df[202])
    with Rung(*_go(slot)):
        calc(dd[222] - 131072, slot.Result1)
        calc(dd[221] - 131072, slot.Result2)
        calc(dd[223] + 65537, slot.Result3)
        calc(df[202] * 100, slot.Result4)
    _fire(slot)


def init_pack_words(slot):
    fill(0, ds.select(4071, 4080))
    fill(0, dd.select(221, 223))
    copy(0, df[202])


def prove_unpack_words(slot):
    with Rung(*_go(slot)):
        copy(1000000, dd[231])
        copy(-2, dd[232])
    with Rung(*_go(slot)):
        unpack_to_words(dd[231], ds.select(4081, 4082))
        unpack_to_words(dd[232], ds.select(4083, 4084))
    with Rung(*_go(slot)):
        copy(ds[4081], slot.Result1)
        copy(ds[4082], slot.Result2)
        copy(ds[4083], slot.Result3)
        copy(ds[4084], slot.Result4)
    _fire(slot)


def init_unpack_words(slot):
    fill(0, ds.select(4081, 4090))
    fill(0, dd.select(231, 232))


group = Group(
    "block",
    [
        case(
            "blockcopy copies element i of the source to element i of the destination "
            "and writes nothing past it: DS {11, 22, 33, 44} copied to 4 registers "
            "with a sentinel 5 after them gives 11 and 22 first (Result1, Result2), "
            "44 last (Result3), sentinel still 5 (Result4)",
            ladder=prove_blockcopy_range,
            init=init_blockcopy_range,
            expect={"Result1": 11, "Result2": 22, "Result3": 44, "Result4": 5},
        ),
        case(
            "blockcopy reads its whole source before it writes: DS {1, 2, 3} copied onto "
            "the registers one past it ({1, 2, 3, 4} -> {1, 1, 2, 3}) reads 1, 1, 2, 3",
            ladder=prove_blockcopy_overlap,
            init=init_blockcopy_overlap,
            expect={"Result1": 1, "Result2": 1, "Result3": 2, "Result4": 3},
        ),
        case(
            "blockcopy converts element by element like copy: DS {-2, 300} into DD "
            "stays -2 and 300 (Result1, Result2), DD {70000, -70000} into DS clamps "
            "to 32767 and -32768 (Result3, Result4)",
            ladder=prove_blockcopy_types,
            init=init_blockcopy_types,
            expect={"Result1": -2, "Result2": 300, "Result3": 32767, "Result4": -32768},
        ),
        case(
            "fill writes its constant to every register of the range and none past it: "
            "7 into 6 registers gives 7 at the first and last (Result1, Result2), the "
            "sentinel after them stays 1 (Result3), and -3 into DD gives -3 (Result4)",
            ladder=prove_fill_constant,
            init=init_fill_constant,
            expect={"Result1": 7, "Result2": 7, "Result3": 1, "Result4": -3},
        ),
        case(
            "blockcopy, fill and copy run on every scan their rung is true: a "
            "destination overwritten with 99 on scan 3 still reads 99 at the start of "
            "scan 4 (Result4), and on scan 4 the instructions have written their "
            "values again: 11, 7 and 5 (Result1, Result2, Result3)",
            ladder=prove_every_scan,
            init=init_every_scan,
            expect={"Result1": 11, "Result2": 7, "Result3": 5, "Result4": 99},
            scans=SCANS,
        ),
        case(
            "pack_bits puts the first bit of the range in bit 0 and overwrites the whole "
            "destination: with C1201, C1202, C1204 and C1216 on, an 8-bit range packs "
            "to 11 over a destination holding 0x7F00 (Result1), and the 16-bit range "
            "packs to -32757, bit 15 being the sign (Result2)",
            ladder=prove_pack_bits_int,
            init=init_pack_bits_int,
            expect={"Result1": 11, "Result2": -32757},
        ),
        case(
            "pack_bits into a REAL stores the raw IEEE-754 bits: bits 21 and 30 on "
            "make 0x40200000, which reads back as low word 0 (Result1), high word "
            "0x4020 = 16416 (Result2), and the Real 2.5 (Result3 = 25 as x10)",
            ladder=prove_pack_bits_real,
            init=init_pack_bits_real,
            expect={"Result1": 0, "Result2": 16416, "Result3": 25},
        ),
        case(
            "unpack_to_bits sends bit 0 to the first bit of the range: 0x8005 (bits 0, "
            "2 and 15) turns on the 1st (Result1 = 1) and 3rd (Result3 = 1) bits, "
            "not the 2nd (Result2 = 0), and the 16th (Result4 = 1)",
            ladder=prove_unpack_bits,
            init=init_unpack_bits,
            expect={"Result1": 1, "Result2": 0, "Result3": 1, "Result4": 1},
        ),
        case(
            "pack_words puts its first source in the low word and does not sign-extend "
            "it: {1, 2} packs to 131073 (Result1 = 1 as minus 131072), {-1, 1} to "
            "131071 (Result2 = -1), {0, -1} to -65536 (Result3 = 1 as plus 65537), "
            "and {0, 16256} into a Real is 1.0 (Result4 = 100 as x100)",
            ladder=prove_pack_words,
            init=init_pack_words,
            expect={"Result1": 1, "Result2": -1, "Result3": 1, "Result4": 100},
        ),
        case(
            "unpack_to_words puts the low word first: 1000000 (0x000F4240) gives "
            "16960 and 15 (Result1, Result2), and -2 (0xFFFFFFFE) gives -2 and -1 "
            "(Result3, Result4), the low word wrapped into an Int, not clamped",
            ladder=prove_unpack_words,
            init=init_unpack_words,
            expect={"Result1": 16960, "Result2": 15, "Result3": -2, "Result4": -1},
        ),
    ],
)


def test_block():
    assert_all_passed(run(group))
