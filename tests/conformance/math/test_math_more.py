"""What a Math box does past the recorded forms: the ``math_more`` and ``hex_math`` groups.

Part ``math`` (``suite.PARTS``).  ``numeric``, ``faults`` and ``store_probe`` read a Math's
wrap, its wide intermediates and its Inf; none reads the shapes below.  An expectation
is pyrung's reading today, and a CLICK that reads otherwise fails and prints what it read,
which is the answer.  Each case runs once (``probe_kit``), so a flag is read on the rung
right after its op.

``math_more``:

- ``int_div_mid_formula``: DS 7 / 2 * 2 into an Int (Result1) is 7 if the intermediate keeps
  the half, 6 if integer division cuts it.  * 10 instead (Result2): 35 or 30.  7 / 2 into a
  DF, then * 10 (Result3): 35 or 30.  DD 7 / 2 * 2 (Result4): 7 or 6.  Bare 7 / 2 into an Int
  (ErrorCode): 3 truncating, 4 rounding.
- ``real_past_int``: a Real past an Int's range, by Math.  DF 40000.0 reads -25536 wrapped;
  DF 1.0E10 -7168, DF -1.0E10 7168 (Result1..3); a clamp reads 32767, 32767, -32768; 0 for no
  store.  Result4 bits 1, 2, 4 are SC43 after each.  DF 1.0E10 into a DD (ErrorCode): 1 for
  1410065408 (wrap), 2 for 2147483647 (clamp), 3 for 0.
- ``abs_and_precision``: DD 2147483647 ^ 2 less itself less 1 (Result1): 1 exact, 0 in a double.
  a * a * 4 / (a * a) (Result2): 4, or another if the intermediate wraps at 64 bits.
  ABS(-32768) into an Int (Result3): -32768 wrapped, 32767 clamped; Result4 bit 1 is SC43.
  ABS(-2147483648) into a DD (ErrorCode): 1 for itself, 2 for 2147483647.
- ``mod_signs``: MOD's sign.  -7 MOD 2 (Result1): 1 floored as pyrung, -1 truncated as C.  7
  MOD -2 (Result2): -1 or 1.  7.5 MOD 2.0 * 10 (Result3): 15.  -7.5 MOD 2.0 * 10 (Result4): 5
  floored, -15 truncated.
- ``power_edges``: 0 ^ 0 (Result1): 1 (0 where a CLICK takes it for an error).  (-2) ^ 3
  (Result2): -8; 2 ^ -1 into a DF * 10 (Result3): 5.  2 ^ -1 into an Int (Result4): 0 cut, 1
  rounded.  2 ^ 40 into an Int (ErrorCode): 0 wrapped, 32767 clamped.  Each of those two starts at
  99, which stays where the store is refused.
- ``math_errors``: what errors the recorded seven leave out, each on a scan its flag starts
  off (``GuardedProbes``), the destination staged at 99: 0 ^ -1 into a DF (Result1, by 1: 0 or
  99 kept); -8.0 ^ 0.5 (Result2); 7 MOD 0 (Result3); 7.5 MOD 0.0 (Result4).  ErrorCode bits: 1
  and 2 SC46 after the first two, 4 and 8 SC40 after the last two, 16 SC40 after the first,
  which pyrung takes for a division by 0.
- ``functions``: radians or degrees, and which log.  SIN(1), COS(1), TAN(1), ATAN(1) and LN(10),
  each * 1000: 841, 540, 1557, 785, 2302 in radians.  In degrees 17, 999, 17, 785 and the rest
  tells.  ``log`` is LN (2302) and not LOG (1000).
- ``wide_constant_into_dint`` (last of the group: the editor may refuse 3000000000, past a DD's
  range, at paste, and a refusal costs only this case): recorded for an Int destination (``numeric/int_math_wide_
  constant``).  Into a DD: DD 131072 / 65536 is 2, or 0 with SC40 where the constant is cut to
  16 bits (Result1 = 1 for 2, 2 for 0; ErrorCode bit 1 is SC40); DD 2 * 40000 is 80000 (1) or
  -51072 (2) (Result2); the same into a DF (Result3); 3000000000 / DD 1000 is 3000000 (1) or
  -1294967 (2) (Result4).

``hex_math`` (WORD only: ``numeric`` reads hex Math's store, not these):

- ``shifts``: LSH on a DH: 0x8001 << 1 (Result1: 1 for 0x0002, the top bit gone; 2 for 0x0003, a
  rotate; 0 for any other); << 16 and << 17 (Result2, by digits: 1 for 0 and 2 for
  anything else); RSH 0xFFFF >> 1 (Result3): 1 logical 0x7FFF, 2 arithmetic 0xFFFF; RSH 0x8000
  >> 16 (Result4): 1 for 0, 2 for unchanged.  ErrorCode: SC43 after the first shift, bit 1,
  after 0xFFFF + 1, bit 2, after 1 - 2, bit 4.
- ``rotates_and_bits``: LRO 0x8001 by 1 (Result1, 1 for 0x0003), by 16 (Result2, 1 for itself,
  2 for 0), by 17 (Result3, 1 for 0x0003).  RRO 0x8001 by 2 (Result4, 1 for 0x6000).
  ErrorCode digits: units, 0xFFFF MOD 0x0100 (1 for 0x00FF, unsigned); tens, 0xF0F0 AND 0xFF00
  XOR 0x0F00 (1 for 0xFF00); hundreds, 0xFFFF / 0 SC40 with 0 (1).

The cases own DD4..DD25, DF151..DF169 and DH5..DH33, and C503..C510 for the guarded ones.
"""

from __future__ import annotations

from pyrung.conformance import Group, assert_all_passed, case, run
from pyrung.core import Rung, calc, copy, fill, reset, system
from pyrung.core.expression import atan, cos, log, lro, lsh, rro, rsh, sin, tan
from tests.conformance.memory import c, dd, df, dh
from tests.conformance.probe_kit import GuardedProbes, class_of, finish, flag_bit, once
from tests.conformance.scans import count_scans, done, scans_for

SC40 = system.fault.division_error
SC43 = system.fault.out_of_range
SC46 = system.fault.math_operation_error


def prove_int_div_mid_formula(slot):
    with Rung(*once(slot)):
        copy(7, slot.Scratch)
        copy(7, dd[4])
        calc(slot.Scratch / 2 * 2, slot.Result1)
        calc(slot.Scratch / 2 * 10, slot.Result2)
        calc(slot.Scratch / 2, df[151])
        calc(df[151] * 10, slot.Result3)
        calc(dd[4] / 2 * 2, slot.Result4)
        calc(slot.Scratch / 2, slot.ErrorCode)
    finish(slot)


def init_int_div_mid_formula(slot):
    copy(0, dd[4])
    copy(0.0, df[151])


def prove_wide_constant_into_dint(slot):
    with Rung(*once(slot)):
        copy(131072, dd[5])
        copy(2, dd[6])
        copy(1000, dd[7])
        calc(dd[5] / 65536, dd[8])
    flag_bit(slot, SC40, slot.ErrorCode, 1)
    with Rung(*once(slot)):
        calc(dd[6] * 40000, dd[9])
        calc(dd[6] * 40000, df[152])
        calc(3000000000 / dd[7], dd[10])
    class_of(slot, dd[8], [2, 0], slot.Result1)
    class_of(slot, dd[9], [80000, -51072], slot.Result2)
    class_of(slot, df[152], [80000.0, -51072.0], slot.Result3)
    class_of(slot, dd[10], [3000000, -1294967], slot.Result4)
    finish(slot)


def init_wide_constant_into_dint(slot):
    fill(0, dd.select(5, 10))
    copy(0.0, df[152])


def prove_real_past_int(slot):
    with Rung(*once(slot)):
        copy(40000.0, df[153])
        copy(1.0e10, df[154])
        copy(-1.0e10, df[155])
        calc(df[153], slot.Result1)
    flag_bit(slot, SC43, slot.Result4, 1)
    with Rung(*once(slot)):
        calc(df[154], slot.Result2)
    flag_bit(slot, SC43, slot.Result4, 2)
    with Rung(*once(slot)):
        calc(df[155], slot.Result3)
    flag_bit(slot, SC43, slot.Result4, 4)
    with Rung(*once(slot)):
        calc(df[154], dd[11])
    class_of(slot, dd[11], [1410065408, 2147483647, 0], slot.ErrorCode)
    finish(slot)


def init_real_past_int(slot):
    fill(0.0, df.select(153, 155))
    copy(0, dd[11])


def prove_abs_and_precision(slot):
    with Rung(*once(slot)):
        copy(2147483647, dd[12])
        copy(-32768, dd[13])
        copy(-2147483648, dd[14])
        calc(dd[12] * dd[12] - (dd[12] * dd[12] - 1), slot.Result1)
        calc(dd[12] * dd[12] * 4 / (dd[12] * dd[12]), slot.Result2)
        copy(-32768, slot.Scratch)
        calc(abs(slot.Scratch), slot.Result3)
    flag_bit(slot, SC43, slot.Result4, 1)
    with Rung(*once(slot)):
        calc(abs(dd[14]), dd[15])
    class_of(slot, dd[15], [-2147483648, 2147483647], slot.ErrorCode)
    finish(slot)


def init_abs_and_precision(slot):
    fill(0, dd.select(12, 15))


def prove_mod_signs(slot):
    with Rung(*once(slot)):
        copy(-7, dd[16])
        copy(7, dd[17])
        copy(-2, dd[18])
        copy(7.5, df[156])
        copy(-7.5, df[157])
        calc(dd[16] % 2, slot.Result1)
        calc(dd[17] % dd[18], slot.Result2)
        calc(df[156] % 2.0 * 10, slot.Result3)
        calc(df[157] % 2.0 * 10, slot.Result4)
    finish(slot)


def init_mod_signs(slot):
    fill(0, dd.select(16, 18))
    fill(0.0, df.select(156, 157))


def prove_power_edges(slot):
    with Rung(*once(slot)):
        copy(0, dd[19])
        copy(-2, dd[20])
        copy(2, dd[21])
        copy(-1, dd[22])
        copy(40, dd[23])
        copy(99, slot.Result4)
        copy(99, slot.ErrorCode)
        calc(dd[19] ** dd[19], slot.Result1)
        calc(dd[20] ** 3, slot.Result2)
        calc(dd[21] ** dd[22], df[158])
        calc(df[158] * 10, slot.Result3)
        calc(dd[21] ** dd[22], slot.Result4)
        calc(dd[21] ** dd[23], slot.ErrorCode)
    finish(slot)


def init_power_edges(slot):
    fill(0, dd.select(19, 23))
    copy(0.0, df[158])


def prove_math_errors(slot):
    probe = GuardedProbes((c[n] for n in range(503, 511)), stage_scan=1, window=8)
    count_scans(slot)
    with Rung(slot.Scratch == 1):
        copy(0.0, df[159])
        copy(-1.0, df[160])
        copy(-8.0, df[161])
        copy(0.5, df[162])
        copy(7, dd[24])
        copy(0, dd[25])
        copy(7.5, df[163])
        copy(0.0, df[164])
        copy(99.0, df[165])
        copy(99.0, df[166])
        copy(99, slot.Result3)
        copy(99.0, df[167])

    def zero_power() -> None:
        calc(df[159] ** df[160], df[165])

    def root_of_minus() -> None:
        calc(df[161] ** df[162], df[166])

    def mod_zero() -> None:
        calc(dd[24] % dd[25], slot.Result3)

    def real_mod_zero() -> None:
        calc(df[163] % df[164], df[167])

    probe(slot, zero_power, SC46, slot.ErrorCode, 1, also=[(SC40, 16)])
    probe(slot, root_of_minus, SC46, slot.ErrorCode, 2)
    probe(slot, mod_zero, SC40, slot.ErrorCode, 4)
    probe(slot, real_mod_zero, SC40, slot.ErrorCode, 8)
    with Rung(slot.Scratch == 8):
        calc(df[165], slot.Result1)
        calc(df[166], slot.Result2)
        calc(df[167], slot.Result4)
    done(slot)


def init_math_errors(slot):
    fill(0.0, df.select(159, 167))
    fill(0, dd.select(24, 25))
    for n in range(503, 511):
        reset(c[n])


def prove_functions(slot):
    with Rung(*once(slot)):
        copy(1.0, df[168])
        copy(10.0, df[169])
        calc(sin(df[168]) * 1000, slot.Result1)
        calc(cos(df[168]) * 1000, slot.Result2)
        calc(tan(df[168]) * 1000, slot.Result3)
        calc(atan(df[168]) * 1000, slot.Result4)
        calc(log(df[169]) * 1000, slot.ErrorCode)
    finish(slot)


def init_functions(slot):
    fill(0.0, df.select(168, 169))


def prove_shifts(slot):
    with Rung(*once(slot)):
        copy(0x8001, dh[5])
        copy(0xFFFF, dh[6])
        copy(0x8000, dh[7])
        copy(1, dh[8])
        copy(2, dh[9])
        calc(lsh(dh[5], 1), dh[10])
    flag_bit(slot, SC43, slot.ErrorCode, 1)
    with Rung(*once(slot)):
        calc(lsh(dh[5], 16), dh[11])
        calc(lsh(dh[5], 17), dh[12])
        calc(rsh(dh[6], 1), dh[13])
        calc(rsh(dh[7], 16), dh[14])
    class_of(slot, dh[10], [0x0002, 0x0003], slot.Result1)
    with Rung(*once(slot), dh[11] != 0):
        calc(slot.Result2 + 20, slot.Result2)
    with Rung(*once(slot), dh[11] == 0):
        calc(slot.Result2 + 10, slot.Result2)
    with Rung(*once(slot), dh[12] != 0):
        calc(slot.Result2 + 2, slot.Result2)
    with Rung(*once(slot), dh[12] == 0):
        calc(slot.Result2 + 1, slot.Result2)
    class_of(slot, dh[13], [0x7FFF, 0xFFFF], slot.Result3)
    class_of(slot, dh[14], [0, 0x8000], slot.Result4)
    with Rung(*once(slot)):
        calc(dh[6] + dh[8], dh[15])
    flag_bit(slot, SC43, slot.ErrorCode, 2)
    with Rung(*once(slot)):
        calc(dh[8] - dh[9], dh[16])
    flag_bit(slot, SC43, slot.ErrorCode, 4)
    finish(slot)


def init_shifts(slot):
    fill(0, dh.select(5, 16))


def prove_rotates_and_bits(slot):
    with Rung(*once(slot)):
        copy(0x8001, dh[20])
        copy(0xFFFF, dh[21])
        copy(0x0100, dh[22])
        copy(0xF0F0, dh[23])
        copy(0xFF00, dh[24])
        copy(0x0F00, dh[25])
        calc(lro(dh[20], 1), dh[26])
        calc(lro(dh[20], 16), dh[27])
        calc(lro(dh[20], 17), dh[28])
        calc(rro(dh[20], 2), dh[29])
        calc(dh[21] % dh[22], dh[30])
        calc(dh[23] & dh[24] ^ dh[25], dh[31])
        copy(0x1234, dh[32])
        calc(dh[21] / dh[33], dh[32])
    with Rung(*once(slot), SC40, dh[32] == 0):
        calc(slot.ErrorCode + 100, slot.ErrorCode)
    class_of(slot, dh[26], [0x0003], slot.Result1)
    class_of(slot, dh[27], [0x8001, 0], slot.Result2)
    class_of(slot, dh[28], [0x0003], slot.Result3)
    class_of(slot, dh[29], [0x6000], slot.Result4)
    with Rung(*once(slot), dh[30] == 0x00FF):
        calc(slot.ErrorCode + 1, slot.ErrorCode)
    with Rung(*once(slot), dh[31] == 0xFF00):
        calc(slot.ErrorCode + 10, slot.ErrorCode)
    finish(slot)


def init_rotates_and_bits(slot):
    fill(0, dh.select(20, 33))


group = Group(
    "math_more",
    [
        case(
            "DS 7 / 2 * 2 keeps the half, 7 (Result1); * 10 is 35 (Result2); 7 / 2 into a DF * 10 "
            "is 35 (Result3); DD 7 / 2 * 2 is 7 (Result4); bare 7 / 2 into an Int is 3 (ErrorCode)",
            ladder=prove_int_div_mid_formula,
            init=init_int_div_mid_formula,
            expect={"Result1": 7, "Result2": 35, "Result3": 35, "Result4": 7, "ErrorCode": 3},
            probe="does a Math's integer division keep the half mid-formula",
        ),
        case(
            "A Real past an Int's range by Math wraps: 40000.0 is -25536 (Result1), 1.0E10 is "
            "-7168 (Result2), -1.0E10 is 7168 (Result3), SC43 each (Result4 = 7); into a DD "
            "1410065408 is neither clamp nor 0 (ErrorCode)",
            ladder=prove_real_past_int,
            init=init_real_past_int,
            expect={
                "Result1": -25536,
                "Result2": -7168,
                "Result3": 7168,
                "Result4": 7,
                "ErrorCode": 1,
            },
            probe="what a Math stores for a Real past an Int's or a Dint's range",
        ),
        case(
            "Integer intermediates: DD 2147483647 squared less itself less 1 is 1 (Result1), a * a "
            "* 4 / (a * a) is 4 (Result2); ABS(-32768) into an Int (Result3, SC43 Result4); "
            "ABS(-2147483648) into a DD (ErrorCode)",
            ladder=prove_abs_and_precision,
            init=init_abs_and_precision,
            expect={"Result1": 1, "Result2": 4, "Result3": -32768, "Result4": 1, "ErrorCode": 1},
            probe="exact or double intermediates; ABS at the bottom of a signed range",
        ),
        case(
            "MOD takes the divisor's sign: -7 MOD 2 is 1 (Result1), 7 MOD -2 is -1 (Result2), "
            "7.5 MOD 2.0 * 10 is 15 (Result3), -7.5 MOD 2.0 * 10 is 5 (Result4)",
            ladder=prove_mod_signs,
            init=init_mod_signs,
            expect={"Result1": 1, "Result2": -1, "Result3": 15, "Result4": 5},
            probe="floored or truncated MOD, Int and Real",
        ),
        case(
            "Powers: 0 ^ 0 is 1 (Result1), (-2) ^ 3 is -8 (Result2), 2 ^ -1 into a DF * 10 is 5 "
            "(Result3), into an Int 0 (Result4), 2 ^ 40 into an Int wraps to 0 (ErrorCode)",
            ladder=prove_power_edges,
            init=init_power_edges,
            expect={"Result1": 1, "Result2": -8, "Result3": 5, "Result4": 0, "ErrorCode": 0},
            probe="power edge values",
        ),
        case(
            "Math errors past the recorded seven, each over a staged 99: 0.0 ^ -1.0 reads 0 with "
            "SC40 (Result1, ErrorCode bit 16), -8.0 ^ 0.5 reads 0 with SC46 (Result2, bit 2); 7 "
            "MOD 0 (Result3) and 7.5 MOD 0.0 (Result4) read 0 with SC40 (bits 4, 8)",
            ladder=prove_math_errors,
            init=init_math_errors,
            scans=scans_for(),
            expect={"Result1": 0, "Result2": 0, "Result3": 0, "Result4": 0, "ErrorCode": 30},
            probe="which flag and what value for 0 ^ -1, a root of a minus, MOD by 0",
        ),
        case(
            "The math functions take radians and log is the natural log: SIN(1), COS(1), TAN(1), "
            "ATAN(1), LN(10), each * 1000, read 841, 540, 1557, 785, 2302",
            ladder=prove_functions,
            init=init_functions,
            expect={
                "Result1": 841,
                "Result2": 540,
                "Result3": 1557,
                "Result4": 785,
                "ErrorCode": 2302,
            },
            probe="radians or degrees; a Math's log forms",
        ),
        case(
            "A constant past 16 bits into a DD or DF is whole: 131072 / 65536 is 2 (Result1 = 1, "
            "no SC40, ErrorCode = 0), 2 * 40000 is 80000 into a DD and a DF (Result2, Result3 = "
            "1), 3000000000 / 1000 is 3000000 (Result4 = 1)",
            ladder=prove_wide_constant_into_dint,
            init=init_wide_constant_into_dint,
            expect={"Result1": 1, "Result2": 1, "Result3": 1, "Result4": 1, "ErrorCode": 0},
            probe="does a wide constant reach a Dint or Real Math whole",
        ),
    ],
)

hex_group = Group(
    "hex_math",
    [
        case(
            "Hex shifts on a DH: 0x8001 << 1 is 0x0002 (Result1 = 1); << 16 and << 17 are 0 "
            "(Result2 = 11); >> 1 of 0xFFFF is logical (Result3 = 1); >> 16 of 0x8000 is 0 "
            "(Result4 = 1); SC43 bits after the shift, 0xFFFF + 1 and 1 - 2 (ErrorCode)",
            ladder=prove_shifts,
            init=init_shifts,
            expect={"Result1": 1, "Result2": 11, "Result3": 1, "Result4": 1, "ErrorCode": 7},
            probe="LSH and RSH at the edges; SC43 in hex Math",
        ),
        case(
            "Hex rotates: LRO 0x8001 by 1 is 0x0003 (Result1 = 1), by 16 itself (Result2 = 1), by "
            "17 as by 1 (Result3 = 1); RRO by 2 is 0x6000 (Result4 = 1); hex MOD is unsigned, AND "
            "and XOR, and / 0 is 0 with SC40 (ErrorCode = 111)",
            ladder=prove_rotates_and_bits,
            init=init_rotates_and_bits,
            expect={"Result1": 1, "Result2": 1, "Result3": 1, "Result4": 1, "ErrorCode": 111},
            probe="LRO and RRO past 16; hex MOD, AND, XOR, division by 0",
        ),
    ],
)


def test_math_more():
    assert_all_passed(run(group))


def test_hex_math():
    assert_all_passed(run(hex_group))
