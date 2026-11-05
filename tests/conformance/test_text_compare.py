"""TXT against a literal and TXT against TXT: the ``text_compare`` group (part P).

CLICK's Compare dialog takes ``TXT1 = "T"`` and ``TXT1 = TXT2`` and offers only = and not-equal
on text; the exporter refuses an order compare (``CLK_TEXT_ORDER_COMPARE``), so none is asked.
A TXT holds NUL until written (``text/null_ascii``, on hardware), but what = and not-equal make
of a blank has not been read.  An expectation is pyrung's reading; a CLICK that reads otherwise
prints what it read, and that is the answer.

Each compare is a decimal digit, 1 where it passed power.  A pair is two digits, = then not-equal:
10 reads equal, 1 unequal, and 11 or 0 says the two are not complements on that value.

- ``txt_against_literal_and_txt``: nothing blank.  Result1, a TXT holding "T" against the literal
  "T" and a TXT holding "U" against it, = then not-equal: 1001.  Result2, the same against a TXT
  holding "T": 1001.
- ``never_written``: a blank no ladder wrote, against "T" (Result1) and a TXT holding "T" (Result2,
  Result3 with the sides swapped): 1, unequal.  Against another never written: 10 (Result4).  Its
  ASCII code (ErrorCode): 0, as ``text/null_ascii``.  Anything else there means the address held
  something, from an earlier project, and the case reads that and not a blank.
- ``cleared``: a TXT written "Q" and then the blank, the same four, with a never written one for
  the last: 1, 1, 10, 10, and its ASCII code 0.
- ``blank_and_space``: a compare could read a blank as a space.  Never written and cleared against " " (Result1, Result2) and a TXT
  holding " " (Result3, Result4): 1 where a blank is not a space, 10 where it is.  ErrorCode, a TXT
  holding " " against " ": 10, the control.
- ``blank_literal``: ``""`` is how the exporter spells the blank.  Never written and cleared against
  it (Result1, Result2): 10.  A TXT holding "T" against it (Result3): 1.  The editor may refuse the
  empty literal, which is an answer: delete this case's rows, the last of the subroutine.  The
  group's hash is a constant in its first rung, so the other cases still run.

The part keeps to TXT addresses no group of the whole suite uses, so no project of the suite has
written the ones never written here.

The cases own what ``memory.OWNED_APART`` lists under this group.
"""

from __future__ import annotations

from typing import Any

from pyrung.conformance import Group, assert_all_passed, case, run
from pyrung.core import Rung, calc, copy, fill, to_ascii
from tests.conformance.memory import OWNED, OWNED_APART, ranges, txt
from tests.conformance.probe_kit import finish, once

BLANK = "\x00"


def _digit(slot: Any, into: Any, weight: int, compare: Any) -> None:
    """Add *weight* to ``into`` where *compare* passes power."""
    with Rung(*once(slot), compare):
        calc(into + weight, into)


def _both(slot: Any, into: Any, left: Any, right: Any) -> None:
    """``into``: 10 where ``left = right`` passes power, plus 1 where not-equal does."""
    _digit(slot, into, 10, left == right)
    _digit(slot, into, 1, left != right)


def _four(slot: Any, into: Any, holds_t: Any, holds_u: Any, right: Any) -> None:
    """= on the equal pair and the unequal pair, then not-equal on each: 1001."""
    _digit(slot, into, 1000, holds_t == right)
    _digit(slot, into, 100, holds_u == right)
    _digit(slot, into, 10, holds_t != right)
    _digit(slot, into, 1, holds_u != right)


def prove_txt_against_literal_and_txt(slot: Any) -> None:
    with Rung(*once(slot)):
        copy("T", txt[801])
        copy("U", txt[802])
        copy("T", txt[803])
    _four(slot, slot.Result1, txt[801], txt[802], "T")
    _four(slot, slot.Result2, txt[801], txt[802], txt[803])
    finish(slot)


def init_txt_against_literal_and_txt(slot: Any) -> None:
    fill(BLANK, txt.select(801, 803))


def prove_never_written(slot: Any) -> None:
    never, other, holds_t = txt[811], txt[812], txt[813]
    with Rung(*once(slot)):
        copy("T", holds_t)
    _both(slot, slot.Result1, never, "T")
    _both(slot, slot.Result2, never, holds_t)
    _both(slot, slot.Result3, holds_t, never)
    _both(slot, slot.Result4, never, other)
    with Rung(*once(slot)):
        copy(never, slot.ErrorCode, convert=to_ascii)
    finish(slot)


def init_never_written(slot: Any) -> None:
    copy(BLANK, txt[813])


def prove_cleared(slot: Any) -> None:
    cleared, other, holds_t, never = txt[821], txt[822], txt[823], txt[824]
    with Rung(*once(slot)):
        copy("Q", cleared)
        copy("Q", other)
        copy(BLANK, cleared)
        copy(BLANK, other)
        copy("T", holds_t)
    _both(slot, slot.Result1, cleared, "T")
    _both(slot, slot.Result2, cleared, holds_t)
    _both(slot, slot.Result3, cleared, other)
    _both(slot, slot.Result4, cleared, never)
    with Rung(*once(slot)):
        copy(cleared, slot.ErrorCode, convert=to_ascii)
    finish(slot)


def init_cleared(slot: Any) -> None:
    fill(BLANK, txt.select(821, 823))


def prove_blank_and_space(slot: Any) -> None:
    never, cleared, space = txt[831], txt[832], txt[833]
    with Rung(*once(slot)):
        copy("Q", cleared)
        copy(BLANK, cleared)
        copy(" ", space)
    _both(slot, slot.Result1, never, " ")
    _both(slot, slot.Result2, cleared, " ")
    _both(slot, slot.Result3, never, space)
    _both(slot, slot.Result4, cleared, space)
    _both(slot, slot.ErrorCode, space, " ")
    finish(slot)


def init_blank_and_space(slot: Any) -> None:
    fill(BLANK, txt.select(832, 833))


def prove_blank_literal(slot: Any) -> None:
    never, cleared, holds_t = txt[841], txt[842], txt[843]
    with Rung(*once(slot)):
        copy("Q", cleared)
        copy(BLANK, cleared)
        copy("T", holds_t)
    _both(slot, slot.Result1, never, BLANK)
    _both(slot, slot.Result2, cleared, BLANK)
    _both(slot, slot.Result3, holds_t, BLANK)
    finish(slot)


def init_blank_literal(slot: Any) -> None:
    fill(BLANK, txt.select(842, 843))


group = Group(
    "text_compare",
    [
        case(
            'A TXT compares by its character: "T" = "T" and "U" not-equal "T" pass, the other two '
            "do not, against a literal (Result1 = 1001) and against a TXT (Result2 = 1001)",
            ladder=prove_txt_against_literal_and_txt,
            init=init_txt_against_literal_and_txt,
            expect={"Result1": 1001, "Result2": 1001},
            probe="TXT = and not-equal, against a literal and a TXT",
        ),
        case(
            'A never written TXT is unequal to "T" (Result1 = 1) and a TXT holding it (Result2 = 1, '
            "swapped Result3 = 1) and equal to another never written (Result4 = 10); ASCII 0 "
            "(ErrorCode)",
            ladder=prove_never_written,
            init=init_never_written,
            expect={"Result1": 1, "Result2": 1, "Result3": 1, "Result4": 10, "ErrorCode": 0},
            probe="what = and not-equal make of a never written TXT",
        ),
        case(
            'A TXT written the blank is unequal to "T" (Result1 = 1) and a TXT holding it (Result2 '
            "= 1) and equal to another cleared (Result3 = 10) and a never written (Result4 = 10); "
            "ASCII 0 (ErrorCode)",
            ladder=prove_cleared,
            init=init_cleared,
            expect={"Result1": 1, "Result2": 1, "Result3": 10, "Result4": 10, "ErrorCode": 0},
            probe="what = and not-equal make of a cleared TXT; is it a never written one",
        ),
        case(
            'A blank is not a space: never written and cleared against " " (Result1 = 1, Result2 '
            '= 1) and a TXT holding " " (Result3 = 1, Result4 = 1); control " " = " " (ErrorCode = '
            "10)",
            ladder=prove_blank_and_space,
            init=init_blank_and_space,
            expect={"Result1": 1, "Result2": 1, "Result3": 1, "Result4": 1, "ErrorCode": 10},
            probe="does a compare read a blank as a space",
        ),
        case(
            'The empty literal is the blank: never written and cleared equal "" (Result1 = 10, '
            'Result2 = 10), a TXT holding "T" does not (Result3 = 1)',
            ladder=prove_blank_literal,
            init=init_blank_literal,
            expect={"Result1": 10, "Result2": 10, "Result3": 1},
            probe="a compare with the empty literal; a refusal at paste is an answer",
        ),
    ],
)


def test_text_compare():
    assert_all_passed(run(group))


def test_the_part_is_on_no_txt_of_the_suite():
    mine = {n for owned in ranges(OWNED_APART["text_compare"]["TXT"]) for n in owned}
    for name, banks in OWNED.items():
        for owned in ranges(banks.get("TXT", ())):
            assert not mine.intersection(owned), name
