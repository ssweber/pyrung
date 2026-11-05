"""A Copy into a Word from below an Int and from NaN: the ``word_floor`` group (part
``follow_timing``, where there is room).

``word_store/word_below_zero`` read on a CLICK that DD -1 copied into a DH writes 65535, the
wrap.  It does not say what a value below -32768 writes, where a store could wrap all 16 bits or
first floor the value at the least Int, nor what a NaN writes.  The fix step ``store-click`` (3 in
the ``conform-fix`` arc) guesses both and waits on this.

``below_int_and_nan``: on one scan, each into a DH preset to 0x1234 (4660), so a store that is
refused reads apart from one that writes:

- Result1, DD -40000: 1 for 25536 (all 16 bits wrap), 2 for 32768 (floored at -32768, then
  wrapped), 3 for 0 (clamped at 0), 4 for 4660 (no store), 0 for any other.
- Result2, DD -70000: 1 for 61072 (all 16 bits wrap), 2 for 32768 (floored), 3 for 0, 4 for
  4660, 0 for any other.
- Result3, a DF holding NaN (+Inf - +Inf, as ``store_probe/inf_and_nan_to_text`` made one): 1
  for 0, 2 for 4660 (no store), 3 for 65535, 4 for 32768, 5 for 32767, 0 for any other.

The cases own what ``memory.OWNED_APART`` lists under this group.
"""

from __future__ import annotations

from typing import Any

from pyrung.conformance import Group, assert_all_passed, case, run
from pyrung.core import Rung, calc, copy, fill
from tests.conformance.memory import dd, df, dh
from tests.conformance.probe_kit import between, class_of, finish, once

SENTINEL = 0x1234


def prove_below_int_and_nan(slot: Any) -> None:
    minus_40000, minus_70000 = dd[91], dd[92]
    inf, nan = df[31], df[32]
    words = [dh[n] for n in (21, 22, 23)]
    with Rung(*once(slot)):
        copy(-40000, minus_40000)
        copy(-70000, minus_70000)
        copy(3.0e38, inf)
        calc(inf * 10, inf)
        calc(inf - inf, nan)
        for word in words:
            copy(SENTINEL, word)
        copy(minus_40000, words[0])
        copy(minus_70000, words[1])
        copy(nan, words[2])
    class_of(slot, words[0], [25536, 32768, 0, SENTINEL], slot.Result1)
    class_of(slot, words[1], [61072, 32768, 0, SENTINEL], slot.Result2)
    class_of(slot, words[2], [0, SENTINEL, 65535, 32768, 32767], slot.Result3)
    finish(slot)


def init_below_int_and_nan(slot: Any) -> None:
    fill(0, dd.select(91, 92))
    fill(0.0, df.select(31, 32))
    fill(0, dh.select(21, 23))


group = Group(
    "word_floor",
    [
        case(
            "A Copy into a DH preset 4660: DD -40000 writes 25536, all 16 bits wrapped "
            "(Result1 = 1, 2 for 32768 floored at -32768, 3 for 0, 4 for none); DD -70000 "
            "61072 (Result2, as Result1); a DF NaN 0 (Result3 = 1, 2 none, 3 65535, 4 32768, "
            "5 32767)",
            ladder=prove_below_int_and_nan,
            init=init_below_int_and_nan,
            expect={
                "Result1": between(0, 4),
                "Result2": between(0, 4),
                "Result3": between(0, 5),
            },
            probe="a Copy into a Word below -32768 and from NaN: wrap, floor or 0 (3)",
        ),
    ],
)


def test_word_floor():
    assert_all_passed(run(group))
