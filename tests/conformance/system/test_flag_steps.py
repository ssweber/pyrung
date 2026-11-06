"""What leaves a fault flag on, read by a coil: the ``flag_steps`` and ``flag_reads`` groups.

Parts ``follow2_flags`` (``flag_steps``) and ``follow2_values`` (``flag_reads``, its first group).

The 2026-11-06 round read SC44 off after a Search, a Shift, a Drum, a TON and a CTU
(``flag_boxes/boxes_by_pointer`` 31/0/0/7) and off in ``ton_ctu_by_pointer``'s control (1/1/1),
where the confirmed ``system/address_flag_vs_other_instructions`` reads it left on after a TON and
a CTU.  One construction explains all of it (H): ``flag_boxes`` read a flag with a Math on a rung
``Scratch == scan, flag``, so the read rungs of earlier scans sat between a raise and its read as
Maths on false rungs, and H says a Math on a false rung clears SC44.  Every reading that had no
false Math between the raise and the read found the flag on.

So here every flag is read by an Out coil on the rung right after the step, with nothing between
raise, step and read, and the coils are recorded into the slot (a Pack, or a Copy) only once every
probe of the scan has run: the layout of ``system/address_flag_vs_other_instructions``.  Each field
holds every candidate (``probe_kit.between``), so a pass answers nothing and a run on a CLICK
records what it read; a control is exact.

``flag_steps`` (F1), one case a flag.  The flag is raised on scans 3 and 4 right before each step,
and the step's coil is a bit of Result1 (scan 3) and Result2 (scan 4), set when the flag is still
on after the step; Result3 is scan 5, with no raise: 0 on every model, the coils follow the flag.

- ``sc44_steps``: SC44 raised by a Copy through a pointer of 0 (there is no DD0).  The steps, bits 1
  to 512: none (the control), a false-rung ``DS + 1`` Math, a false-rung ``copy(1, DS)``, a
  false-rung Copy through a good pointer, a false-rung Math with an SC44 contact on its rung (the
  shape of ``flag_boxes``' read rungs), a Search into a DS Result, a Shift, an event Drum, a TON and
  a CTU, the last five enabled on every scan of the window.  The readings:

  - H, a false Math clears SC44 and nothing else here does: 1005 (all but bits 2 and 16).
  - The boxes clear SC44 ("4b"): 31 (none and the four false rungs).
  - Neither (pyrung): 1023.
  - A false Copy clears it too: bits 4 and 8 off as well.
- ``sc43_steps``: the same steps after SC43, raised by a Copy of DD 40000 into a DS (it clamps,
  ``faults/copy_clamp_out_of_range``).  The same readings: H 1005 if it holds for SC43 too, the
  boxes 31, pyrung 1023.
- ``sc40_steps``: SC40, raised by a division by 0, and the Math steps only (a Copy and the boxes
  leave SC40, ``system/division_flag_vs_hex_math``): none, a false-rung ``DS + 1`` Math, a
  false-rung Math with an SC40 contact; bits 1, 2, 4.  H for SC40: 1; pyrung: 7.

``flag_reads``, the questions the audit left beside it:

- ``unpowered_division`` (F2): does a Math on a rung that is never true evaluate its formula, or
  clear a flag?  ``1 / DS`` (DS 0) on a false rung, then a coil read of SC40.  Result1, scan 3: the
  flag off right before (a clean Math settles it), on after the false Math only if an unpowered
  Math still evaluates (raises SC40).  Result2, scan 4: SC40 raised by a true division right
  before, off after if the false Math clears it (H), on if it leaves it (or raises it again).
  Result3, scan 5, is the control, the raise alone: 1.  pyrung: 0, 1, 1.  H: 0, 0.
- ``oneshot_held`` (F3): a oneshot Copy and a oneshot Math held on over scans 3 and 4, so they fire
  on 3 and not on 4, each with its flag raised right before.  Bits: SC44 then the Copy (1), SC44
  then the Math (2), SC43 then the Copy (4), SC43 then the Math (8), SC40 then the Math (16).
  Result1, scan 4: set when the flag is still on after the oneshot that does not fire.  31: it
  leaves them (H, no false rung between); 0: a held oneshot settles the flags every scan its rung
  is on (pyrung, and what ``flag_probe/oneshot_that_does_not_fire`` read behind a false Math).
  Result3, scan 3: the same when it fires, 0 (a Copy or a Math that runs settles them).  Result2,
  the controls, each raise alone: 7.  pyrung: 0, 7, 0.
- ``pointer_minus_one`` (F4): SC44 raised by a pointer of -1, read straight after by a coil (bit
  1), after a TON (bit 2) and after a CTU (bit 4), enabled every scan of the window; Result1 scan
  3, Result2 scan 4.  ``ton_ctu_by_pointer`` read the -1 raise off behind a false Math; H: 7, 7,
  and pyrung 7, 7.  0 for bit 1: a pointer of -1 raises no SC44.  Result3, the pointer of 0 read
  straight after on scan 3, is the control: 1.
- ``math_after_sqrt`` (F5): SC46 from ``SQRT(DF -1.0)`` on scan 3, then an enabled ``DS + 1``
  Math, then a coil.  Result1 = 1: the Math leaves SC46 (pyrung, "SC46 for the rest of the scan");
  0: a Math settles SC46 as it does SC40.  Result2 is the control, the root alone on scan 5: 1.
  Result3, SC46 at the top of scan 3 before the root: 0 (nothing raised it).  Result4, SC46 on
  scan 4 after the same steps with no root: 0 where it clears at the scan's start or a Math
  settles it, 1 where it carries over (``flag_carry`` asks that alone).  pyrung: 1, 1, 0, 0.

No case here raises SC46 but ``math_after_sqrt``, and every read follows its own raise.  In part
``follow2_values`` ``flag_reads`` runs first, above ``term_zero``, whose quiet errors may raise
SC46 or SC40 on a CLICK: no flag of a later group sits between a raise here and its read.  The cases own what ``memory.OWNED_APART`` lists under
these groups.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from pyrung.conformance import Group, assert_all_passed, case, run
from pyrung.core import (
    Or,
    Rung,
    copy,
    count_up,
    event_drum,
    fill,
    math,
    on_delay,
    out,
    pack_bits,
    reset,
    search,
    shift,
    system,
)
from pyrung.core.expression import sqrt
from tests.conformance.memory import ClickCounter, ClickTimer, c, dd, df, ds
from tests.conformance.probe_kit import between
from tests.conformance.scans import WINDOW, count_scans, done, scans_for

SC40 = system.fault.division_error
SC43 = system.fault.out_of_range
SC44 = system.fault.address_error
SC46 = system.fault.math_operation_error

#: The scans each case raises its flag on, and the scan after, with no raise.
RAISE_SCANS = (3, 4)
QUIET_SCAN = 5
#: ``Scratch`` never reaches it (it holds at ``WINDOW + 1``): a rung false on every scan.
NEVER = 99


def _raising(slot: Any) -> tuple[Any, ...]:
    return (slot.Cmd != 0, Or(*[slot.Scratch == n for n in RAISE_SCANS]))


def _in_window(slot: Any) -> tuple[Any, ...]:
    return (slot.Cmd != 0, slot.Scratch <= WINDOW)


def _read(slot: Any, flag: Any, coil: Any) -> None:
    """The coil read: *coil* on while *flag* is, right after the step."""
    with Rung(*_in_window(slot), flag):
        out(coil)


def _pack(slot: Any, scan: int, first: int, last: int, field: Any) -> None:
    """*field* = the coils C<first>..C<last> as bits, on *scan*, below every probe."""
    with Rung(slot.Cmd != 0, slot.Scratch == scan):
        pack_bits(c.select(first, last), field)


# --- flag_steps (F1) ----------------------------------------------------------------


class _Steps:
    """A case's memory, cut for the ten steps of ``flag_steps``.

    C<base>..C<base+9> are the coils, C<base+10>.. the steps' own bits; DS<ds>.. the steps'
    registers and DD<dd>.. their sinks.
    """

    def __init__(self, base: int, ds_base: int, dd_base: int, timer: int, counter: int) -> None:
        self.coils = range(base, base + 10)
        self.bits = range(base + 10, base + 26)
        self.ds = range(ds_base, ds_base + 12)
        self.dd = range(dd_base, dd_base + 4)
        self.timer, self.counter = timer, counter

    def ds_n(self, i: int) -> Any:
        return ds[self.ds[i]]

    def dd_n(self, i: int) -> Any:
        return dd[self.dd[i]]

    def bit(self, i: int) -> Any:
        return c[self.bits[i]]

    #: DS offsets: the pointer of 0, the good pointer, three step registers, a Search range of
    #: four, its Result, the drum's step.
    ZERO, GOOD, MATH, COPY, CONTACT, RANGE, RESULT, STEP = 0, 1, 2, 3, 4, 5, 9, 10

    def good_target(self) -> int:
        """The DD the good pointer names: the last of the case's DDs."""
        return self.dd[3]

    def steps(self, slot: Any, flag: Any) -> list[Callable[[], None] | None]:
        """None, four false rungs, then five boxes enabled over the window."""
        never = slot.Scratch == NEVER

        def false_math() -> None:
            with Rung(never):
                math(self.ds_n(self.MATH) + 1, self.ds_n(self.MATH))

        def false_copy() -> None:
            with Rung(never):
                copy(1, self.ds_n(self.COPY))

        def false_pointer_copy() -> None:
            with Rung(never):
                copy(dd[self.ds_n(self.GOOD)], self.dd_n(1))

        def false_math_with_contact() -> None:
            with Rung(never, flag):
                math(self.ds_n(self.CONTACT) + 1, self.ds_n(self.CONTACT))

        def box(build: Callable[[], object]) -> Callable[[], None]:
            def step() -> None:
                with Rung(*_in_window(slot)):
                    build()

            return step

        rng = self.ds[self.RANGE]
        return [
            None,
            false_math,
            false_copy,
            false_pointer_copy,
            false_math_with_contact,
            box(
                lambda: search(
                    ds.select(rng, rng + 3) == 99,
                    result=self.ds_n(self.RESULT),
                    found=self.bit(0),
                )
            ),
            box(
                lambda: (
                    shift(c.select(self.bits[1], self.bits[8]))
                    .clock(self.bit(9))
                    .reset(slot.Cmd == 0)
                )
            ),
            box(
                lambda: event_drum(
                    outputs=[self.bit(10), self.bit(11)],
                    events=[self.bit(12), self.bit(13)],
                    pattern=[[1, 0], [0, 1]],
                    current_step=self.ds_n(self.STEP),
                    completion_flag=self.bit(14),
                ).reset(slot.Cmd == 0)
            ),
            box(lambda: on_delay(ClickTimer(self.timer), 30000)),
            box(lambda: count_up(ClickCounter(self.counter), 1000).reset(slot.Cmd == 0)),
        ]

    def init(self) -> None:
        reset(c.select(self.coils.start, self.bits.stop - 1))
        fill(0, ds.select(self.ds.start, self.ds.stop - 1))
        fill(0, dd.select(self.dd.start, self.dd.stop - 1))
        copy(self.good_target(), self.ds_n(self.GOOD))


def _flag_steps(
    slot: Any,
    flag: Any,
    raise_flag: Callable[[], None],
    steps: list[Callable[[], None] | None],
    coils: range,
) -> None:
    """Before each step the raise, after it the coil; the coils recorded below them all."""
    count_scans(slot)
    for step, coil in zip(steps, coils, strict=True):
        with Rung(*_raising(slot)):
            raise_flag()
        if step is not None:
            step()
        _read(slot, flag, c[coil])
    last = coils.start + len(steps) - 1
    fields = (slot.Result1, slot.Result2, slot.Result3)
    for scan, field in zip((*RAISE_SCANS, QUIET_SCAN), fields, strict=True):
        _pack(slot, scan, coils.start, last, field)
    done(slot)


SC44_MEM = _Steps(501, 3951, 101, timer=251, counter=3)
SC43_MEM = _Steps(531, 3963, 105, timer=252, counter=4)
#: SC43's raise: DD 40000 copied into a DS.
SC43_BIG, SC43_CLAMPED = dd[109], ds[3975]


def prove_sc44_steps(slot: Any) -> None:
    m = SC44_MEM
    _flag_steps(
        slot,
        SC44,
        lambda: copy(dd[m.ds_n(m.ZERO)], m.dd_n(0)),  # no DD0: SC44
        m.steps(slot, SC44),
        m.coils,
    )


def init_sc44_steps(slot: Any) -> None:
    SC44_MEM.init()


def prove_sc43_steps(slot: Any) -> None:
    m = SC43_MEM
    _flag_steps(
        slot,
        SC43,
        lambda: copy(SC43_BIG, SC43_CLAMPED),  # 40000 clamps into an Int: SC43
        m.steps(slot, SC43),
        m.coils,
    )


def init_sc43_steps(slot: Any) -> None:
    SC43_MEM.init()
    copy(0, SC43_CLAMPED)
    copy(40000, SC43_BIG)


#: sc40_steps: C561..C563 the coils; DS3976 the divisor (0), DS3977..3978 the steps'; DD110 sink.
SC40_COILS = range(561, 564)
SC40_ZERO, SC40_MATH, SC40_CONTACT, SC40_SINK = ds[3976], ds[3977], ds[3978], dd[110]


def prove_sc40_steps(slot: Any) -> None:
    never = slot.Scratch == NEVER

    def false_math() -> None:
        with Rung(never):
            math(SC40_MATH + 1, SC40_MATH)

    def false_math_with_contact() -> None:
        with Rung(never, SC40):
            math(SC40_CONTACT + 1, SC40_CONTACT)

    _flag_steps(
        slot,
        SC40,
        lambda: math(slot.Scratch / SC40_ZERO, SC40_SINK),  # a division by 0: SC40
        [None, false_math, false_math_with_contact],
        SC40_COILS,
    )


def init_sc40_steps(slot: Any) -> None:
    reset(c.select(SC40_COILS.start, SC40_COILS.stop - 1))
    fill(0, ds.select(3976, 3978))
    copy(0, SC40_SINK)


def _steps_case(name: str, ladder: Any, init: Any, top: int, what: str) -> Any:
    return case(
        f"{name} raised right before each step, read by a coil right after: a bit per step "
        f"still on after it on scan 3 (Result1) and 4 (Result2), {what}; none on scan 5 with no "
        "raise (Result3 = 0)",
        ladder=ladder,
        init=init,
        expect={
            "Result1": between(0, top),
            "Result2": between(0, top),
            "Result3": 0,
        },
        scans=scans_for(),
        probe=f"what clears {name}: a false Math (H), a box, or nothing (4b)",
    )


flag_steps = Group(
    "flag_steps",
    [
        _steps_case(
            "SC44",
            prove_sc44_steps,
            init_sc44_steps,
            1023,
            "1023 where none clears it, 1005 where a false Math does (H), 31 where the boxes do",
        ),
        _steps_case(
            "SC43",
            prove_sc43_steps,
            init_sc43_steps,
            1023,
            "1023 where none clears it, 1005 where a false Math does (H), 31 where the boxes do",
        ),
        _steps_case(
            "SC40",
            prove_sc40_steps,
            init_sc40_steps,
            7,
            "7 where neither false Math clears it, 1 where both do (H)",
        ),
    ],
)


# --- flag_reads (F2..F5) -------------------------------------------------------------


#: unpowered_division: C571..C573 the coils; DS3979 the divisor (0), DS3980 the clean Math's; DD111..
UNPOWERED_COILS = range(571, 574)


def prove_unpowered_division(slot: Any) -> None:
    clean_coil, cleared_coil, control_coil = (c[n] for n in UNPOWERED_COILS)
    zero, clean = ds[3979], ds[3980]
    never = slot.Scratch == NEVER
    count_scans(slot)
    # Scan 3: SC40 settled by a clean Math, then the division on a false rung.
    with Rung(slot.Cmd != 0, slot.Scratch == 3):
        math(slot.Scratch + 1, clean)
    with Rung(never):
        math(1 / zero, dd[111])
    _read(slot, SC40, clean_coil)
    # Scan 4: SC40 raised by a true division, then the division on a false rung.
    with Rung(slot.Cmd != 0, slot.Scratch == 4):
        math(1 / zero, dd[112])
    with Rung(never):
        math(1 / zero, dd[113])
    _read(slot, SC40, cleared_coil)
    # Scan 5: the raise alone.
    with Rung(slot.Cmd != 0, slot.Scratch == 5):
        math(1 / zero, dd[114])
    _read(slot, SC40, control_coil)
    for scan, coil, field in (
        (3, clean_coil, slot.Result1),
        (4, cleared_coil, slot.Result2),
        (5, control_coil, slot.Result3),
    ):
        with Rung(slot.Cmd != 0, slot.Scratch == scan, coil):
            copy(1, field)
    done(slot)


def init_unpowered_division(slot: Any) -> None:
    reset(c.select(UNPOWERED_COILS.start, UNPOWERED_COILS.stop - 1))
    fill(0, ds.select(3979, 3980))
    fill(0, dd.select(111, 114))


#: oneshot_held: C581..C585 the probes' coils, C586..C588 the controls'; DS3981..3990, DD115..
ONESHOT_COILS = range(581, 589)


def prove_oneshot_held(slot: Any) -> None:
    zero, clamped, divisor = ds[3981], ds[3982], ds[3983]
    big = dd[115]
    raises = {
        SC44: lambda: copy(dd[zero], dd[116]),  # no DD0: SC44
        SC43: lambda: copy(big, clamped),  # 40000 clamps into an Int: SC43
        SC40: lambda: math(slot.Scratch / divisor, dd[117]),  # a division by 0: SC40
    }
    ops: list[tuple[Any, Callable[[], object]]] = [
        (SC44, lambda: copy(1, ds[3984], oneshot=True)),
        (SC44, lambda: math(ds[3985] + 1, ds[3985], oneshot=True)),
        (SC43, lambda: copy(1, ds[3986], oneshot=True)),
        (SC43, lambda: math(ds[3987] + 1, ds[3987], oneshot=True)),
        (SC40, lambda: math(ds[3988] + 1, ds[3988], oneshot=True)),
    ]
    coils = [c[n] for n in ONESHOT_COILS]
    count_scans(slot)
    held = (slot.Cmd != 0, slot.Scratch >= 3, slot.Scratch <= 4)
    for (flag, op), coil in zip(ops, coils[:5], strict=True):
        with Rung(*_raising(slot)):
            raises[flag]()
        with Rung(*held):
            op()
        _read(slot, flag, coil)
    for flag, coil in zip((SC44, SC43, SC40), coils[5:], strict=True):
        with Rung(*_raising(slot)):
            raises[flag]()
        _read(slot, flag, coil)
    first = ONESHOT_COILS.start
    _pack(slot, 4, first, first + 4, slot.Result1)
    _pack(slot, 4, first + 5, first + 7, slot.Result2)
    _pack(slot, 3, first, first + 4, slot.Result3)
    done(slot)


def init_oneshot_held(slot: Any) -> None:
    reset(c.select(ONESHOT_COILS.start, ONESHOT_COILS.stop - 1))
    fill(0, ds.select(3981, 3988))
    fill(0, dd.select(116, 117))
    copy(40000, dd[115])


#: pointer_minus_one: C591..C593 the probes' coils, C594 the control's; DS3991..3992, DD118..
MINUS_COILS = range(591, 595)


def prove_pointer_minus_one(slot: Any) -> None:
    minus, zero = ds[3991], ds[3992]
    coils = [c[n] for n in MINUS_COILS]

    def by_minus() -> None:
        copy(dd[minus], dd[118])  # a pointer of -1: SC44

    count_scans(slot)
    with Rung(*_raising(slot)):
        by_minus()
    _read(slot, SC44, coils[0])
    with Rung(*_raising(slot)):
        by_minus()
    with Rung(*_in_window(slot)):
        on_delay(ClickTimer(253), 30000)
    _read(slot, SC44, coils[1])
    with Rung(*_raising(slot)):
        by_minus()
    with Rung(*_in_window(slot)):
        count_up(ClickCounter(5), 1000).reset(slot.Cmd == 0)
    _read(slot, SC44, coils[2])
    with Rung(*_raising(slot)):
        copy(dd[zero], dd[119])  # no DD0: SC44
    _read(slot, SC44, coils[3])
    first = MINUS_COILS.start
    _pack(slot, 3, first, first + 2, slot.Result1)
    _pack(slot, 4, first, first + 2, slot.Result2)
    with Rung(slot.Cmd != 0, slot.Scratch == 3, coils[3]):
        copy(1, slot.Result3)
    done(slot)


def init_pointer_minus_one(slot: Any) -> None:
    reset(c.select(MINUS_COILS.start, MINUS_COILS.stop - 1))
    fill(0, dd.select(118, 119))
    copy(0, ds[3992])
    copy(-1, ds[3991])


#: math_after_sqrt: C601..C604 (before, after, control); DF51..53, DS3993.
SQRT_COILS = range(601, 604)


def prove_math_after_sqrt(slot: Any) -> None:
    before, after, control = (c[n] for n in SQRT_COILS)
    minus, counted = df[51], ds[3993]
    count_scans(slot)
    _read(slot, SC46, before)
    with Rung(slot.Cmd != 0, slot.Scratch == 3):
        math(sqrt(minus), df[52])
    with Rung(slot.Cmd != 0, Or(slot.Scratch == 3, slot.Scratch == 4)):
        math(counted + 1, counted)
    _read(slot, SC46, after)
    with Rung(slot.Cmd != 0, slot.Scratch == 5):
        math(sqrt(minus), df[53])
    _read(slot, SC46, control)
    for scan, coil, field in (
        (3, after, slot.Result1),
        (5, control, slot.Result2),
        (3, before, slot.Result3),
        (4, after, slot.Result4),
    ):
        with Rung(slot.Cmd != 0, slot.Scratch == scan, coil):
            copy(1, field)
    done(slot)


def init_math_after_sqrt(slot: Any) -> None:
    reset(c.select(SQRT_COILS.start, SQRT_COILS.stop - 1))
    fill(0.0, df.select(52, 53))
    copy(-1.0, df[51])
    copy(0, ds[3993])


flag_reads = Group(
    "flag_reads",
    [
        case(
            "A division by 0 on a rung that is never true, read by a coil: SC40 on after it from "
            "off (Result1 = 1, it evaluates), still on after it from a raise (Result2 = 1, it "
            "leaves the flag; 0 it clears it); the raise alone leaves SC40 on (Result3 = 1)",
            ladder=prove_unpowered_division,
            init=init_unpowered_division,
            expect={"Result1": between(0, 1), "Result2": between(0, 1), "Result3": 1},
            scans=scans_for(),
            probe="does a Math on a false rung evaluate, or clear SC40 (4b)",
        ),
        case(
            "A oneshot Copy and Math held on, right after a raise of SC44 (bits 1, 2), SC43 (4, "
            "8) or SC40 (16): a bit per op still on after it on the scan it does not fire "
            "(Result1), and on the scan it fires (Result3 = 0); each raise alone (Result2 = 7)",
            ladder=prove_oneshot_held,
            init=init_oneshot_held,
            expect={"Result1": between(0, 31), "Result2": 7, "Result3": 0},
            scans=scans_for(),
            probe="does a oneshot that does not fire settle its flags (4b)",
        ),
        case(
            "SC44 raised by a pointer of -1 is on right after (bit 1), after a TON (2) and a CTU "
            "(4), on scan 3 (Result1) and 4 (Result2); by a pointer of 0 (Result3 = 1)",
            ladder=prove_pointer_minus_one,
            init=init_pointer_minus_one,
            expect={"Result1": between(0, 7), "Result2": between(0, 7), "Result3": 1},
            scans=scans_for(),
            probe="SC44 by a pointer of -1 read straight after (4b)",
        ),
        case(
            "SC46 from SQRT(-1.0) is still on after an enabled Math (Result1 = 1, 0 it settles); "
            "the root alone (Result2 = 1); off before the root (Result3 = 0); on the next scan "
            "with no root (Result4 = 1 it carries over)",
            ladder=prove_math_after_sqrt,
            init=init_math_after_sqrt,
            expect={
                "Result1": between(0, 1),
                "Result2": 1,
                "Result3": 0,
                "Result4": between(0, 1),
            },
            scans=scans_for(),
            probe="does a Math that runs settle SC46",
        ),
    ],
)


def test_flag_steps():
    assert_all_passed(run(flag_steps))


def test_flag_reads():
    assert_all_passed(run(flag_reads))
