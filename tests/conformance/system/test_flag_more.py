"""Follow-up flag probes: the ``flag_boxes`` and ``flag_halves`` groups (part ``follow_flags``).

The 2026-11-05 round left two holes in ``flag_probe`` (part ``system``), and the fix step
``flags-settle`` (4b in the ``conform-fix`` arc) waits on both:

- ``flag_probe/boxes_leave_flags`` read SC44 off after a Search, a Shift, a Drum, a TON and a
  CTU, where the confirmed ``system/address_flag_vs_other_instructions`` reads SC44 left on after
  a TON and a CTU.  The first raised SC44 with a pointer of -1, the second with a pointer of 0.
- Every SC43 half of ``flag_probe`` said nothing: it raised SC43 with ``copy(DF 1.0E+10, DD)``,
  which raises no SC43 on a CLICK.  Here SC43 comes from a Copy of DD 40000 into a DS, which
  does (``faults/copy_clamp_out_of_range``, confirmed).

Each probe raises its flag right before the instruction under test and reads it on the rung
after, as ``flag_probe`` does, so another group's rungs on the same scan cannot reach it, and
each case has a control: the raise alone, read the same way, so a raise that does not take shows
as a control that reads 0.  A question's field holds every candidate (``probe_kit.between``), so
a pass answers nothing and a run on a CLICK records what it read; a control is exact.

``flag_boxes``:

- ``boxes_by_pointer``: ``flag_probe``'s layout, each box on a rung of its own true for three
  scans: SC43 raised on its first, SC44 by pointer 0 on its second, SC44 by pointer -1 on its
  third (as ``boxes_leave_flags`` raised it).  Bits 1, 2, 4, 8, 16 for the Search, Shift, Drum,
  TON and CTU, set when the flag is still on after the box: Result1 SC43, Result2 SC44 by 0,
  Result3 SC44 by -1.  pyrung reads 31, 31, 31.  The round read 0 for Result3; Result2 = 31 says
  the pointer decides, 0 that the box clears SC44 however it was raised.  Result4 is the control:
  bits 1, 2, 4 for the three raises alone, 7.
- ``ton_ctu_by_pointer``: ``address_flag_vs_other_instructions``'s layout, the TON and the CTU
  enabled on every scan of the window and the raise on the rung above each: pointer 0 on scan 3
  (bit 1), pointer -1 on scan 4 (bit 2), set when SC44 is still on after it.  Result2 the TON,
  Result3 the CTU; pyrung 3, 3.  A CLICK that reads 1 there clears a -1 raise only; 3, that
  ``boxes_by_pointer`` differs by its layout.  Result1 is the control, 3.

``flag_halves``, the SC43 halves of ``flag_probe`` with a raise that takes:

- ``bulk_ops_settle_sc43``: a Block copy, Fill, Pack (bits, words, text) and Unpack (bits, words)
  right after the raise.  Result1 bits 1..64, set when SC43 is off after each; pyrung 127, and
  127 is what SC44 read (``bulk_ops_clear_flags``).  Result2 = 1 is the control.
- ``oneshot_settles_sc43``: a oneshot Copy (bit 1) and a oneshot Math (bit 2), on the scan it
  fires (Result2, set when SC43 is off after it) and the next, when it does not fire (Result1,
  set when SC43 is still on).  pyrung 3, 3; SC44 and SC40 read 0 for Result1
  (``oneshot_that_does_not_fire``), a oneshot that does not fire still settles them.  Result3 =
  1 is the control.
- ``source_sc43``: ``copy(SC43, C)`` right after the raise.  Result1 = 1 when the C bit saw SC43
  on (pyrung), 0 when the Copy settles the flag before it reads (SC44 read 0,
  ``source_flag_is_read_before_the_copy_settles``).  Result2 = 1 when SC43 is off after the
  Copy.  Result3 = 1 is the control.
- ``df_into_dd``: what a Copy of DF 1.0E+10 into a DD (preset 99) writes, the store that raised
  no SC43.  Result1: 1 for 2147483647 (a clamp), 2 for 1410065408 (the low 32 bits), 3 for 99
  (left as it was), 4 for 0, 5 for -2147483648, 0 for any other.  Result2 is the DD / 100000, so
  any other reading still shows its size (21474 or 21475 for the clamp, 14100 or 14101 for the
  low bits).  Result3 has SC43 after it (bit 1; the round read none); pyrung reads 1, 21474, 1.

The cases own what ``memory.OWNED_APART`` lists under these groups.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from pyrung.conformance import Group, assert_all_passed, case, run
from pyrung.core import (
    Rung,
    blockcopy,
    calc,
    copy,
    count_up,
    event_drum,
    fill,
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
from tests.conformance.probe_kit import between
from tests.conformance.scans import count_scans, done, scans_for

SC43 = system.fault.out_of_range
SC44 = system.fault.address_error
NUL = "\x00"

#: The scan a case stages its registers on, and its first probe.
STAGE_SCAN = 2
FIRST_PROBE = 3


def _read(slot: Any, scan: int, flag: Any, field: Any, bit: int, *, on: bool = True) -> None:
    """Add *bit* to *field* on *scan* if *flag* is on (``on``) or off right now."""
    with Rung(slot.Scratch == scan, flag if on else ~flag):
        calc(field + bit, field)


# --- flag_boxes -----------------------------------------------------------------

BOX_WINDOW = 20


def prove_boxes_by_pointer(slot: Any) -> None:
    count_scans(slot, BOX_WINDOW)
    big, clamped, minus, zero, sink = dd[1], ds[3905], ds[3906], ds[3907], dd[2]
    with Rung(slot.Scratch == STAGE_SCAN):
        copy(40000, big)
        copy(-1, minus)
        copy(0, zero)
    raises: list[tuple[Callable[[], object], Any]] = [
        (lambda: copy(big, clamped), SC43),  # 40000 clamps into an Int: SC43
        (lambda: copy(dd[zero], sink), SC44),  # no DD0: SC44
        (lambda: copy(dd[minus], sink), SC44),  # a pointer of -1: SC44
    ]
    boxes: list[Callable[[], object]] = [
        lambda: search(dd.select(3, 6) == 99, result=dd[7], found=c[101]),
        lambda: shift(c.select(102, 109)).clock(c[110]).reset(slot.Cmd == 0),
        lambda: event_drum(
            outputs=[c[111], c[112]],
            events=[c[113], c[114]],
            pattern=[[1, 0], [0, 1]],
            current_step=ds[3908],
            completion_flag=c[115],
        ).reset(slot.Cmd == 0),
        lambda: on_delay(ClickTimer(101), 5000),
        lambda: count_up(ClickCounter(1), 3).reset(slot.Cmd == 0),
    ]
    # The controls: each raise alone, on scans 3, 4 and 5.
    for k, (raise_flag, flag) in enumerate(raises):
        with Rung(slot.Scratch == FIRST_PROBE + k):
            raise_flag()
        _read(slot, FIRST_PROBE + k, flag, slot.Result4, 1 << k)
    fields = (slot.Result1, slot.Result2, slot.Result3)
    for j, box in enumerate(boxes):
        first = FIRST_PROBE + 3 + 3 * j
        for k, (raise_flag, _flag) in enumerate(raises):
            with Rung(slot.Scratch == first + k):
                raise_flag()
        with Rung(slot.Scratch >= first, slot.Scratch <= first + 2):
            box()
        for k, ((_raise, flag), field) in enumerate(zip(raises, fields, strict=True)):
            _read(slot, first + k, flag, field, 1 << j)
    done(slot, BOX_WINDOW)


def init_boxes_by_pointer(slot: Any) -> None:
    reset(c.select(101, 115))
    fill(0, dd.select(1, 7))
    fill(0, ds.select(3905, 3908))


def prove_ton_ctu_by_pointer(slot: Any) -> None:
    count_scans(slot)
    minus, zero, sink = ds[3909], ds[3910], dd[8]
    window = (slot.Cmd != 0, slot.Scratch <= 8)
    with Rung(slot.Scratch == 1):
        copy(-1, minus)
        copy(0, zero)

    def ton() -> None:
        with Rung(*window):
            on_delay(ClickTimer(102), 30000)

    def ctu() -> None:
        with Rung(*window):
            count_up(ClickCounter(2), 1000).reset(slot.Cmd == 0)

    steps: list[tuple[Callable[[], None] | None, Any]] = [
        (None, slot.Result1),
        (ton, slot.Result2),
        (ctu, slot.Result3),
    ]
    for step, field in steps:
        with Rung(slot.Cmd != 0, slot.Scratch == 3):
            copy(dd[zero], sink)
        with Rung(slot.Cmd != 0, slot.Scratch == 4):
            copy(dd[minus], sink)
        if step is not None:
            step()
        _read(slot, 3, SC44, field, 1)
        _read(slot, 4, SC44, field, 2)
    done(slot)


def init_ton_ctu_by_pointer(slot: Any) -> None:
    copy(0, dd[8])
    fill(0, ds.select(3909, 3910))


flag_boxes = Group(
    "flag_boxes",
    [
        case(
            "A Search, Shift, event Drum, TON and CTU after SC43 (Result1), SC44 raised by pointer "
            "0 (Result2) and SC44 raised by pointer -1 (Result3): a bit per box still on after "
            "it, 31 where every box leaves the flag; each raise alone leaves it on (Result4 = 7)",
            ladder=prove_boxes_by_pointer,
            init=init_boxes_by_pointer,
            expect={
                "Result1": between(0, 31),
                "Result2": between(0, 31),
                "Result3": between(0, 31),
                "Result4": 7,
            },
            scans=scans_for(BOX_WINDOW),
            probe="do the boxes clear SC43, and SC44 by pointer 0 against -1 (4b)",
        ),
        case(
            "A TON and a CTU enabled every scan, as address_flag_vs_other_instructions has them, "
            "after SC44 raised by pointer 0 (bit 1) and by pointer -1 (bit 2): Result2, Result3 = 3 "
            "where both leave it; the raise alone leaves it on (Result1 = 3)",
            ladder=prove_ton_ctu_by_pointer,
            init=init_ton_ctu_by_pointer,
            expect={"Result1": 3, "Result2": between(0, 3), "Result3": between(0, 3)},
            scans=scans_for(),
            probe="does a TON or CTU clear SC44 raised by pointer -1 but not by 0 (4b)",
        ),
    ],
)


# --- flag_halves ----------------------------------------------------------------

HALF_WINDOW = 12


def _raise_sc43(big: Any, clamped: Any) -> None:
    copy(big, clamped)  # DD 40000 clamps into an Int: SC43 (faults/copy_clamp_out_of_range)


def prove_bulk_ops_settle_sc43(slot: Any) -> None:
    count_scans(slot, HALF_WINDOW)
    big, clamped = dd[21], ds[3911]
    with Rung(slot.Scratch == STAGE_SCAN):
        copy(40000, big)
        copy("1", txt[1])
        copy("2", txt[2])
    ops: list[Callable[[], object]] = [
        lambda: blockcopy(dd.select(22, 23), dd.select(24, 25)),
        lambda: fill(0, dd.select(26, 27)),
        lambda: pack_bits(c.select(141, 156), dd[28]),
        lambda: pack_words(dh.select(1, 2), dd[29]),
        lambda: pack_text(txt.select(1, 2), dd[30]),
        lambda: unpack_to_bits(dh[3], c.select(157, 172)),
        lambda: unpack_to_words(dd[31], dh.select(4, 5)),
    ]
    scan = FIRST_PROBE
    for k, op in enumerate(ops):
        with Rung(slot.Scratch == scan):
            _raise_sc43(big, clamped)
            op()
        _read(slot, scan, SC43, slot.Result1, 1 << k, on=False)
        scan += 1
    with Rung(slot.Scratch == scan):
        _raise_sc43(big, clamped)
    _read(slot, scan, SC43, slot.Result2, 1)
    done(slot, HALF_WINDOW)


def init_bulk_ops_settle_sc43(slot: Any) -> None:
    reset(c.select(141, 172))
    fill(0, dd.select(21, 31))
    fill(0, dh.select(1, 5))
    fill(NUL, txt.select(1, 2))
    copy(0, ds[3911])


def prove_oneshot_settles_sc43(slot: Any) -> None:
    count_scans(slot, HALF_WINDOW)
    big, clamped = dd[32], ds[3912]
    with Rung(slot.Scratch == STAGE_SCAN):
        copy(40000, big)
    probes: list[Callable[[Any], object]] = [
        lambda dest: copy(1, dest, oneshot=True),
        lambda dest: calc(dest + 1, dest, oneshot=True),
    ]
    for k, op in enumerate(probes):
        with Rung(slot.Scratch >= FIRST_PROBE, slot.Scratch <= FIRST_PROBE + 1):
            _raise_sc43(big, clamped)
        with Rung(slot.Scratch >= FIRST_PROBE, slot.Scratch <= FIRST_PROBE + 1):
            op(dd[33 + k])
        _read(slot, FIRST_PROBE, SC43, slot.Result2, 1 << k, on=False)
        _read(slot, FIRST_PROBE + 1, SC43, slot.Result1, 1 << k)
    control = FIRST_PROBE + 2
    with Rung(slot.Scratch == control):
        _raise_sc43(big, clamped)
    _read(slot, control, SC43, slot.Result3, 1)
    done(slot, HALF_WINDOW)


def init_oneshot_settles_sc43(slot: Any) -> None:
    fill(0, dd.select(32, 34))
    copy(0, ds[3912])


def prove_source_sc43(slot: Any) -> None:
    count_scans(slot, HALF_WINDOW)
    big, clamped, seen = dd[35], ds[3913], c[173]
    with Rung(slot.Scratch == STAGE_SCAN):
        copy(40000, big)
    with Rung(slot.Scratch == FIRST_PROBE):
        _raise_sc43(big, clamped)
        copy(SC43, seen)
    with Rung(slot.Scratch == FIRST_PROBE, seen):
        copy(1, slot.Result1)
    with Rung(slot.Scratch == FIRST_PROBE, ~SC43):
        copy(1, slot.Result2)
    control = FIRST_PROBE + 1
    with Rung(slot.Scratch == control):
        _raise_sc43(big, clamped)
    _read(slot, control, SC43, slot.Result3, 1)
    done(slot, HALF_WINDOW)


def init_source_sc43(slot: Any) -> None:
    reset(c[173])
    copy(0, dd[35])
    copy(0, ds[3913])


#: What a Copy of DF 1.0E+10 into a DD may write, in Result1's order.
DF_INTO_DD = (2147483647, 1410065408, 99, 0, -2147483648)


def prove_df_into_dd(slot: Any) -> None:
    count_scans(slot, HALF_WINDOW)
    big, scale, into = df[1], dd[36], dd[37]
    with Rung(slot.Scratch == STAGE_SCAN):
        copy(1.0e10, big)
        copy(100000, scale)
        copy(99, into)
    with Rung(slot.Scratch == FIRST_PROBE):
        copy(big, into)
    _read(slot, FIRST_PROBE, SC43, slot.Result3, 1)
    for number, reading in enumerate(DF_INTO_DD, start=1):
        with Rung(slot.Scratch == FIRST_PROBE + 1, into == reading):
            copy(number, slot.Result1)
    with Rung(slot.Scratch == FIRST_PROBE + 1):
        calc(into / scale, slot.Result2)
    done(slot, HALF_WINDOW)


def init_df_into_dd(slot: Any) -> None:
    copy(0.0, df[1])
    fill(0, dd.select(36, 37))


flag_halves = Group(
    "flag_halves",
    [
        case(
            "A Block copy, Fill, Pack (bits, words, text) or Unpack (bits, words) right after a "
            "Copy that raises SC43 settles it: a bit per op with SC43 off after it, 127 for all "
            "seven (Result1); the raise alone leaves it on (Result2 = 1)",
            ladder=prove_bulk_ops_settle_sc43,
            init=init_bulk_ops_settle_sc43,
            expect={"Result1": between(0, 127), "Result2": 1},
            scans=scans_for(HALF_WINDOW),
            probe="do the bulk Copy tabs settle SC43, with a raise that takes (4b)",
        ),
        case(
            "A oneshot Copy (bit 1) and Math (bit 2) after SC43: off after it on the scan it fires "
            "(Result2 = 3), still on the scan it does not fire (Result1 = 3); the raise alone "
            "leaves SC43 on (Result3 = 1)",
            ladder=prove_oneshot_settles_sc43,
            init=init_oneshot_settles_sc43,
            expect={"Result1": between(0, 3), "Result2": between(0, 3), "Result3": 1},
            scans=scans_for(HALF_WINDOW),
            probe="does a oneshot that does not fire settle SC43 (4b)",
        ),
        case(
            "A Copy whose source is SC43, right after a raise, reads it on (Result1 = 1) and "
            "leaves it off (Result2 = 1); the raise alone leaves SC43 on (Result3 = 1)",
            ladder=prove_source_sc43,
            init=init_source_sc43,
            expect={"Result1": between(0, 1), "Result2": between(0, 1), "Result3": 1},
            scans=scans_for(HALF_WINDOW),
            probe="does a Copy read SC43 before it settles it (4b)",
        ),
        case(
            "What a Copy of DF 1.0E+10 into a DD writes: 2147483647 for a clamp (Result1 = 1, "
            "Result2 = DD / 100000), and SC43 after it (Result3), which the round read off",
            ladder=prove_df_into_dd,
            init=init_df_into_dd,
            expect={
                "Result1": between(0, 5),
                "Result2": between(-21475, 21475),
                "Result3": between(0, 1),
            },
            scans=scans_for(HALF_WINDOW),
            probe="what a Copy of DF 1.0E+10 into a DD writes (4b)",
        ),
    ],
)


def test_flag_boxes():
    assert_all_passed(run(flag_boxes))


def test_flag_halves():
    assert_all_passed(run(flag_halves))
