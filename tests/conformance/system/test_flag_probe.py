"""Flag probes: the ``flag_probe`` group (subroutine ``Conf_FlagProbe``).

Part ``system`` (``suite.PARTS``).  The fault-flag model (``bob/flags``)
chose several things without a reading, and each case here asks a CLICK one of them.
SC40 follows the last Math.  SC43 and SC44 follow the last Copy or Math, and Block
copy, Fill, Pack and Unpack count as Copy tabs.  Nothing else touches a flag.
Answered on a CLICK (2026-11-05): such an instruction clears its flags as it starts,
then reads its operands.  A Copy of DF 1.0E+10 into a DD raises no SC43, so every
SC43 half here raises nothing (the ``flag_halves`` group, part ``follow_flags``,
asks them again with a raise that takes).  ``flag_steps`` and ``flag_reads`` (parts
``follow2_flags``, ``follow2_values``) later found that a Math on a false rung, or a
oneshot Math that does not fire, clears SC40, SC43 and SC44.  A false Copy and a
oneshot Copy that does not fire leave them.

Every probe stands on its own: it raises the flag itself with something a CLICK has
already answered (a Copy that clamps raises SC43, a pointer below its bank SC44, a
division by zero SC40), runs the instruction under test right after, and reads the
flag on the rung after that.  So no other group's rungs on the same scan (every case
of every group runs at once) can change what a probe reads.  A probe has a scan of
its own (``Scratch`` is the scan), and its read is a Math that adds a bit to a
result.  The cases leave a flag up at the end of the scan, so the group runs after
every group that reads one (``suite.ALL_GROUPS``).

A pointer of -1 is staged in a slot field (CLICK takes a pointer in a Single Copy
or a Pack/Unpack tab only) and a DF of 1.0E+10 for the clamp, on scan 2.

- ``bulk_ops_clear_flags`` (probe): does a Block copy, Fill, Pack (bits, words, text)
  or Unpack (bits, words) that succeeds clear a flag a Copy raised?  Only a single
  Copy and a Math were measured.  Result1 has bits 1..64 for the seven ops, in that
  order, set when SC43 is off after it; Result2 the same for SC44.  Result3 is the
  control, the raise alone: bit 1 SC43 still on, bit 2 SC44 (3).  A bit missing in
  Result1 or Result2 is an op that leaves that flag as it was.  CLICK read 127, 127,
  2: the SC43 raise does not take.
- ``oneshot_that_does_not_fire`` (probe): a Copy or Math under a oneshot, on the
  scan it fires and the next, when it does not.  Five probes (bits 1, 2, 4, 8, 16):
  a Copy with SC43 and SC44 raised, a Math with SC40, SC43 and SC44.  Result2 has
  a bit where the oneshot clears the flag the scan it fires, Result1 where the
  flag is still on the scan it does not fire.  CLICK read 0, 31.  Result1 says
  nothing about the oneshot.  The read rung of the scan before sits between the
  oneshot and its read, and as a Math on a false rung it clears every flag.
  ``flag_reads/oneshot_held`` reads it with a coil, and the Copy leaves its flags.
- ``boxes_leave_flags`` (probe): a Search, a Shift, an event Drum, a timer and a
  counter box between the raise and the read.  Bits 1, 2, 4, 8, 16 in that order,
  set when the flag is still on after the box: Result1 SC40, Result2 SC43,
  Result3 SC44.  A bit missing is a flag that is off after the box.  CLICK read 31,
  0, 0.  The SC43 raise does not take.  SC44 reads off because the read rungs of
  the two scans before it are Maths on false rungs, and each clears SC44
  (``flag_steps``).  The boxes leave every flag (``flag_steps``, and the confirmed
  ``system/address_flag_vs_other_instructions``).
- ``source_flag_is_read_before_the_copy_settles`` (probe): a Copy whose source is
  the flag.  After the raise, ``copy(SC43, C)``: Result1 = 1 when C saw it on,
  0 if the Copy settles the flag before it reads its source; Result2 = 1 when
  SC43 is off after the Copy.  Result3, Result4 the same for SC44.  CLICK read
  0, 1, 0, 1: the Copy settles first (the SC43 half raises nothing).
- ``source_flag_in_a_math`` (probe): the same for SC40 in a Math, ``DD =
  SC40 + 5`` after a division by zero: the DD reads 6 if the Math sees the flag
  it is about to settle (Result1; 5 if it sees the flag off), and SC40 is off
  after it (Result2 = 1).  CLICK read 5, 1: the Math settles first.

Not probed, pack_text with bad text and a bad destination pointer (which flag): CLICK
takes a pointer in a Single Copy only (the exporter refuses a Pack text destination
pointer, CLK_PTR_CONTEXT_ONLY_COPY), so it cannot be written.

Not probed here: whether a flag carries over a scan boundary.  The harness's own
rungs get in the way.  The program header Copy and each group's hash Copy clear
SC43 and SC44 first.  Every group runs on the same scan, so another group's Math
clears SC40 or raises SC46 before the reader.  ``flag_carry`` (part
``follow2_carry``) runs alone and reads SC40 and SC46 carried.

Each read here is a Math on a rung true only on its scan.  On every other scan it is
a Math on a false rung, so it clears SC40, SC43 and SC44 too.  The readings above
count that in.

The cases own C1331..C1380, T181, CT147, DD689..DD717, DF430..DF433, DH417..DH421
and TXT501..TXT502 (``memory.OWNED``).
"""

from __future__ import annotations

from collections.abc import Callable

from pyrung.conformance import Group, assert_all_passed, case, run
from pyrung.core import (
    Rung,
    blockcopy,
    copy,
    count_up,
    event_drum,
    fill,
    math,
    on_delay,
    pack_bits,
    pack_text,
    pack_words,
    reset,
    search,
    shift,
    system,
    unpack_to_bits,
    unpack_to_words,
)
from tests.conformance.memory import ClickCounter, ClickTimer, c, dd, df, dh, ds, txt
from tests.conformance.scans import count_scans, done, scans_for

WINDOW = 20
SCANS = scans_for(WINDOW)
STAGE_SCAN = 2
FIRST_PROBE = 3

SC40 = system.fault.division_error
SC43 = system.fault.out_of_range
SC44 = system.fault.address_error
NUL = "\x00"


def _stage(slot, *instructions):
    with Rung(slot.Scratch == STAGE_SCAN):
        for instruction in instructions:
            instruction()


def _read(slot, scan, flag, field, bit, *, on=True):
    """Add *bit* to *field* on *scan* if *flag* is on (``on``) or off right now."""
    with Rung(slot.Scratch == scan, flag if on else ~flag):
        math(field + bit, field)


def _raise(kind, *, big, tmp, zero=dd[689], pointer=None):
    """Raise a flag with an instruction a CLICK has answered: "40", "43" or "44"."""
    if kind == "43":
        copy(big, tmp)  # 1.0E+10 clamps into a Dint
    elif kind == "44":
        copy(dd[pointer], tmp)  # a pointer of -1, below the bank
    else:
        math(100 / zero, tmp)


def _init(*, bits=None, dds=None, dfs=None, dhs=None, txts=None, dss=None):
    def init(slot):
        if bits:
            reset(c.select(*bits))
        if dds:
            fill(0, dd.select(*dds))
        if dfs:
            fill(0.0, df.select(*dfs))
        if dhs:
            fill(0, dh.select(*dhs))
        if txts:
            fill(NUL, txt.select(*txts))
        if dss:
            fill(0, ds.select(*dss))

    return init


# --- 1. Block copy, Fill, Pack and Unpack ---------------------------------------


def prove_bulk_ops_clear_flags(slot):
    count_scans(slot, WINDOW)
    big, tmp, pointer = df[430], dd[689], slot.Result4
    _stage(
        slot,
        lambda: copy(1.0e10, big),
        lambda: copy(-1, pointer),
        lambda: copy("1", txt[501]),
        lambda: copy("2", txt[502]),
    )
    ops: list[Callable[[], object]] = [
        lambda: blockcopy(dd.select(690, 691), dd.select(692, 693)),
        lambda: fill(0, dd.select(694, 695)),
        lambda: pack_bits(c.select(1331, 1346), dd[696]),
        lambda: pack_words(dh.select(417, 418), dd[697]),
        lambda: pack_text(txt.select(501, 502), dd[698]),
        lambda: unpack_to_bits(dh[419], c.select(1347, 1362)),
        lambda: unpack_to_words(dd[699], dh.select(420, 421)),
    ]
    scan = FIRST_PROBE
    for kind, flag, field in (("43", SC43, slot.Result1), ("44", SC44, slot.Result2)):
        for k, op in enumerate(ops):
            with Rung(slot.Scratch == scan):
                _raise(kind, big=big, tmp=tmp, pointer=pointer)
                op()
            _read(slot, scan, flag, field, 1 << k, on=False)
            scan += 1
    for k, (kind, flag) in enumerate((("43", SC43), ("44", SC44))):
        with Rung(slot.Scratch == scan):
            _raise(kind, big=big, tmp=tmp, pointer=pointer)
        _read(slot, scan, flag, slot.Result3, 1 << k)
        scan += 1
    done(slot, WINDOW)


# --- 2. A oneshot that does not fire -------------------------------------------


def prove_oneshot_that_does_not_fire(slot):
    count_scans(slot, WINDOW)
    big, tmp, zero, pointer = df[431], dd[705], dd[706], slot.Result4
    _stage(slot, lambda: copy(1.0e10, big), lambda: copy(-1, pointer))
    probes = [
        ("43", SC43, lambda dest: copy(1, dest, oneshot=True)),
        ("44", SC44, lambda dest: copy(1, dest, oneshot=True)),
        ("40", SC40, lambda dest: math(dest + 1, dest, oneshot=True)),
        ("43", SC43, lambda dest: math(dest + 1, dest, oneshot=True)),
        ("44", SC44, lambda dest: math(dest + 1, dest, oneshot=True)),
    ]
    for k, (kind, flag, op) in enumerate(probes):
        with Rung(slot.Scratch >= FIRST_PROBE, slot.Scratch <= FIRST_PROBE + 1):
            _raise(kind, big=big, tmp=tmp, zero=zero, pointer=pointer)
        with Rung(slot.Scratch >= FIRST_PROBE, slot.Scratch <= FIRST_PROBE + 1):
            op(dd[700 + k])
        _read(slot, FIRST_PROBE, flag, slot.Result2, 1 << k, on=False)
        _read(slot, FIRST_PROBE + 1, flag, slot.Result1, 1 << k)
    done(slot, WINDOW)


# --- 3. Search, Shift, Drum, a timer and a counter ------------------------------


def prove_boxes_leave_flags(slot):
    count_scans(slot, WINDOW)
    big, tmp, zero, pointer = df[432], dd[707], dd[708], slot.Result4
    _stage(slot, lambda: copy(1.0e10, big), lambda: copy(-1, pointer))
    boxes: list[Callable[[], object]] = [
        lambda: search(dd.select(709, 712) == 99, result=ds[4018], found=c[1363]),
        lambda: shift(c.select(1364, 1371)).clock(c[1372]).reset(slot.Cmd == 0),
        lambda: event_drum(
            outputs=[c[1373], c[1374]],
            events=[c[1375], c[1376]],
            pattern=[[1, 0], [0, 1]],
            current_step=slot.ErrorCode,
            completion_flag=c[1377],
        ).reset(slot.Cmd == 0),
        lambda: on_delay(ClickTimer(181), 5000),
        lambda: count_up(ClickCounter(147), 3).reset(slot.Cmd == 0),
    ]
    flags = (("40", SC40, slot.Result1), ("43", SC43, slot.Result2), ("44", SC44, slot.Result3))
    for j, box in enumerate(boxes):
        first = FIRST_PROBE + 3 * j
        for f, (kind, _flag, _field) in enumerate(flags):
            with Rung(slot.Scratch == first + f):
                _raise(kind, big=big, tmp=tmp, zero=zero, pointer=pointer)
        with Rung(slot.Scratch >= first, slot.Scratch <= first + 2):
            box()
        for f, (_kind, flag, field) in enumerate(flags):
            _read(slot, first + f, flag, field, 1 << j)
    done(slot, WINDOW)


# --- 5. A flag read by its own instruction's source -----------------------------


def prove_source_flag_is_read_before_the_copy_settles(slot):
    count_scans(slot, WINDOW)
    big, tmp, pointer = df[433], dd[714], slot.ErrorCode
    _stage(slot, lambda: copy(1.0e10, big), lambda: copy(-1, pointer))
    probes = (
        ("43", SC43, c[1378], slot.Result1, slot.Result2),
        ("44", SC44, c[1379], slot.Result3, slot.Result4),
    )
    for k, (kind, flag, seen, saw, off) in enumerate(probes):
        scan = FIRST_PROBE + k
        with Rung(slot.Scratch == scan):
            _raise(kind, big=big, tmp=tmp, pointer=pointer)
            copy(flag, seen)
        with Rung(slot.Scratch == scan, seen):
            copy(1, saw)
        with Rung(slot.Scratch == scan, ~flag):
            copy(1, off)
    done(slot, WINDOW)


def prove_source_flag_in_a_math(slot):
    count_scans(slot, WINDOW)
    tmp, zero, number = dd[715], dd[716], dd[717]
    with Rung(slot.Scratch == FIRST_PROBE):
        math(100 / zero, tmp)
        math(SC40 + 5, number)
    with Rung(slot.Scratch == FIRST_PROBE, ~SC40):
        copy(1, slot.Result2)
    with Rung(slot.Scratch == FIRST_PROBE):
        copy(number, slot.Result1)
    done(slot, WINDOW)


group = Group(
    "flag_probe",
    [
        case(
            "A Block copy, Fill, Pack (bits, words, text) or Unpack (bits, words) that "
            "succeeds clears SC44 a Copy raised (Result2 = 127); a Copy of DF 1.0E+10 into "
            "a DD raises no SC43, so SC43 is off after each of the seven (Result1 = 127) and "
            "the raise alone leaves only SC44 on (Result3 = 2)",
            ladder=prove_bulk_ops_clear_flags,
            init=_init(
                bits=(1331, 1362), dds=(689, 699), dfs=(430, 430), dhs=(417, 421), txts=(501, 502)
            ),
            expect={"Result1": 127, "Result2": 127, "Result3": 2},
            scans=SCANS,
            probe="do the bulk Copy tabs settle SC43 and SC44 as a single Copy does",
        ),
        case(
            "A oneshot Copy or Math clears its flags on the scan it fires (Result2 = 31).  The "
            "Copies follow a raise of SC43 or SC44, the Maths a raise of SC40, SC43 or SC44.  On "
            "the next scan the oneshot does not fire, and no flag reads on (Result1 = 0).  The "
            "read rung of the scan before sits between the oneshot and that read, and as a Math "
            "on a false rung it clears every flag",
            ladder=prove_oneshot_that_does_not_fire,
            init=_init(dds=(700, 706), dfs=(431, 431)),
            expect={"Result1": 0, "Result2": 31},
            scans=SCANS,
            probe="a oneshot that does not fire: does it run, and settle a flag",
        ),
        case(
            "A Search, Shift, event Drum, timer and counter box leave SC40 as they found it "
            "(Result1 = 31); a Copy of DF 1.0E+10 into a DD raises no SC43, so none is on "
            "after a box (Result2 = 0); the false-rung read Maths of the two scans before clear "
            "SC44 (Result3 = 0)",
            ladder=prove_boxes_leave_flags,
            init=_init(bits=(1363, 1377), dds=(707, 712), dfs=(432, 432), dss=(4018, 4018)),
            expect={"Result1": 31, "Result2": 0, "Result3": 0},
            scans=SCANS,
            probe="do search, shift, drum, timer and counter boxes touch a fault flag",
        ),
        case(
            "A Copy clears SC43 and SC44 as it starts, before it reads its source: "
            "copy(SC44, C) right after a raise reads 0 (Result3 = 0) and SC44 is off after "
            "(Result4 = 1); a Copy of DF 1.0E+10 into a DD raises no SC43, so copy(SC43, C) "
            "reads 0 (Result1 = 0) and SC43 is off (Result2 = 1)",
            ladder=prove_source_flag_is_read_before_the_copy_settles,
            init=_init(bits=(1378, 1379), dds=(714, 714), dfs=(433, 433)),
            expect={"Result1": 0, "Result2": 1, "Result3": 0, "Result4": 1},
            scans=SCANS,
            probe="does a Copy read its source flag before or after it settles the flag",
        ),
        case(
            "A Math clears SC40 as it starts, before it reads its operands: SC40 + 5 after a "
            "division by zero is 5 (Result1) and SC40 is off after it (Result2 = 1)",
            ladder=prove_source_flag_in_a_math,
            init=_init(dds=(715, 717)),
            expect={"Result1": 5, "Result2": 1},
            scans=SCANS,
            probe="does a Math read its operand flag before or after it settles the flag",
        ),
    ],
)


def test_flag_probe():
    assert_all_passed(run(group))
