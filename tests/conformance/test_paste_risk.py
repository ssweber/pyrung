"""Probes a CLICK may refuse at paste: the ``paste_risk`` group (subroutine ``Conf_PasteRisk``).

Part I of the CLICK project (``suite.PARTS``), a project of its own.  Each
case here writes an operand CLICK may not take where it stands (a register
the editor or the project check could refuse), so a refusal at paste,
transfer or project check costs only this small project, and no confirmed
group shares it.  A case CLICK refuses never fires: that is its answer.

The raw registers are the group's own (``memory.OWNED``): C751..C770 and
DF411..DF420.

- ``jump_to_own_step_register`` (probe): a 3-step event drum whose jump step
  register is its own current-step register (Result1).  Its event (C752) goes
  on on scan 2 (step 2) and again on scan 4 with the jump (C753), so on scan 4
  the drum advances and jumps on one scan.  Adopted (pyrung since the drum
  reads its jump step with its pins, before it moves): the jump goes to the
  step the register held when the scan reached the drum (2) and beats the
  advance, so the drum ends on step 2 (Result1 = 2) and step 3 is never
  entered (Result3 = 0, its output C751 never on, Result4 = 0).  A jump to
  the register as it reads after the advance (3, pyrung before that change),
  or a jump to 2 followed by the advance, both leave step 3, entered on scan
  4 (Result3 = 4) with its output on from scan 4 (Result4 = 4): 3, 2, 4, 4.
  Result2 = 2 is step 2's entry scan.  (Moved here from ``drum`` before any
  CLICK ran it.)
- ``rand_real_bounds`` (probe): RAND whose bounds are DF registers, probed as
  the ``faults`` group probes (``test_faults._probe``: the op once, on the
  first scan from 3 its flag is off just before it).  3.0 and 3.0 (DF411)
  into an Int preset 99 (Result1: 3), and SC46 after it (ErrorCode); +Inf and
  +Inf (DF412, 3.0E+38 * 10 staged on scan 2) into an Int preset 99
  (Result2), with SC43 and SC46 after it as bits 1 and 2 of Result3.
  Adopted: as ``numeric/int_math_rand_wide_bounds`` read, a bound the Int
  cannot hold is a Math error that writes 0 with SC46: 3, 0, 2, 0.  pyrung
  (step MATH) writes 0 with SC43 instead: 3, 0, 1, 0.  A CLICK that keeps
  RAND's bounds as integers and refuses a DF never fires the case.  (Moved
  here from ``faults`` before any CLICK ran it.)
"""

from __future__ import annotations

from pyrung.conformance import Group, assert_all_passed, case, run
from pyrung.core import Or, Rung, calc, copy, event_drum, fill, out, reset
from pyrung.core.expression import randint
from tests.conformance.memory import c, df
from tests.conformance.scans import count_scans, done, scans_for
from tests.conformance.test_drum import entered, first_on_from
from tests.conformance.test_faults import (
    SC43,
    SC46,
    _probe,
    _stage,
    _stage_inf,
)
from tests.conformance.test_faults import (
    SCANS as FAULT_SCANS,
)
from tests.conformance.test_faults import (
    WINDOW as FAULT_WINDOW,
)

SCANS = scans_for()

#: The (now, taken) bit pairs of ``rand_real_bounds``'s three probes.
_RAND_BITS = [c[n] for n in range(761, 767)]


def prove_jump_to_own_step_register(slot):
    # The event (C752) of every step is on scans 2 and 4, the jump (C753) on
    # scan 4.  The jump's step register is the drum's current-step register.
    count_scans(slot)
    with Rung(slot.Cmd != 0, Or(slot.Scratch == 2, slot.Scratch == 4)):
        out(c[752])
    with Rung(slot.Cmd != 0, slot.Scratch == 4):
        out(c[753])
    with Rung(slot.Cmd != 0):
        event_drum(
            outputs=[c[751]],
            events=[c[752], c[752], c[752]],
            pattern=[[0], [0], [1]],
            current_step=slot.Result1,
            completion_flag=c[754],
        ).reset(slot.Cmd == 0).jump(c[753], step=slot.Result1)
    entered(slot, 2, slot.Result2)
    entered(slot, 3, slot.Result3)
    first_on_from(slot, c[751], 1, slot.Result4)
    done(slot)


def prove_rand_real_bounds(slot):
    bits = iter(_RAND_BITS)

    def probe(op, flag, field, bit=1):
        _probe(slot, next(bits), next(bits), op, flag, field, bit)

    count_scans(slot, FAULT_WINDOW)
    _stage(slot, lambda: copy(3.0, df[411]), *_stage_inf(df[412]))

    def finite():
        copy(99, slot.Result1)
        calc(randint(df[411], df[411]), slot.Result1)

    def infinite():
        copy(99, slot.Result2)
        calc(randint(df[412], df[412]), slot.Result2)

    probe(finite, SC46, slot.ErrorCode)
    for k, flag in enumerate((SC43, SC46)):
        probe(infinite, flag, slot.Result3, 1 << k)
    done(slot, FAULT_WINDOW)


def init_rand_real_bounds(slot):
    for bit in _RAND_BITS:
        reset(bit)
    fill(0.0, df.select(411, 412))


group = Group(
    "paste_risk",
    [
        case(
            "A drum that advances and jumps on one scan to the step its own step register "
            "holds jumps to the step the register held before the advance: the event "
            "enters step 2 on scan 2 (Result2), and on scan 4 the jump takes it back to "
            "step 2 (Result1), so step 3 is never entered (Result3 = 0) and its output "
            "never on (Result4 = 0)",
            ladder=prove_jump_to_own_step_register,
            probe="jump to the drum's own step register on an advance scan",
            expect={"Result1": 2, "Result2": 2, "Result3": 0, "Result4": 0},
            scans=SCANS,
        ),
        case(
            "RAND takes DF bounds: RAND ( 3.0 , 3.0 ) from two DFs writes 3 into an Int "
            "(Result1) without SC46 (ErrorCode = 0), and RAND of +Inf and +Inf is a Math "
            "error that writes 0 (Result2) with SC46 alone (Result3 = 2)",
            ladder=prove_rand_real_bounds,
            init=init_rand_real_bounds,
            expect={"Result1": 3, "Result2": 0, "Result3": 2, "ErrorCode": 0},
            scans=FAULT_SCANS,
            probe="RAND with a DF bound, finite and +Inf",
            pending="MATH",
        ),
    ],
)


def test_paste_risk():
    assert_all_passed(run(group))
