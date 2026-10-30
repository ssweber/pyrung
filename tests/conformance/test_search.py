"""Table search: the ``search`` group (subroutine ``Conf_Search``).

``search`` walks a range and writes the address of the first element that
satisfies a comparison into a result register, and a found bit.  Tables are
staged in raw DS (DS4201..DS4400), DD401.., DF301.. and TXT201.. that the group
owns (``memory.OWNED``); the found bits are C851..C900.

Every table starts well away from address 1, so a result that is a position in
the range (1, 2, ...) reads differently from the register number.  Every table
has several matches, so first-match reads differently from last-match.
pyrung returns the register number of the first match (``DS4204``, not 2).

The expectations are what a CLICK does (hardware round 1, 2026-10-29, part B:
every case but one passed as written).  A continuous search on a CLICK
restarts at the first address when its result is 0, resumes after an old
result that lies inside the range, and misses (-1) from anything else.
Where a case detects an alternative model:

- ``hit_returns_address``: result = position in the range (2), a 0-based
  position (1), or the last match (4206).
- ``miss_writes_minus_one``: a miss leaves the old result (77) or the old found
  bit on.
- ``operators_signed``: ``>`` / ``<=`` / ``>=`` boundaries (``>`` taken as
  ``>=`` reads 4243 for Result1; ``<=`` taken as ``<`` reads 4244 for Result3;
  ``>=`` taken as ``>`` misses for Result4) and signed compare (``< 0`` misses
  if the words are unsigned).
- ``range_ends_inclusive``: a range that excludes its first or last register
  (or leaks one past it).
- ``false_rung_preserves``: a search that still runs while its rung is false
  (the table is changed after the hit, so it would read a different answer), or
  one that clears found.
- ``continuous_walks``: continuous mode that restarts each scan reads 4303
  four times.
- ``continuous_exhausted``: after the last match the result stays -1 (a wrap
  would read 4323 on scan 4); writing 0 restarts at the first match.
- ``continuous_zero_restarts``: result = 0 restarts the walk mid-way.
- ``plain_search_ignores_result``: a non-continuous search that starts after
  the register's old value (reads 4366) instead of at the top of the range.
- ``continuous_cursor_is_address``: the resume point is a register number,
  strictly after the old result (4386, not 4385), also when the old result is
  not itself a match (4384 -> 4385), -1 past the last register, and -1 when
  the old result lies below the range (5; pyrung restarts there, 4383).
- ``continuous_cursor_outside_range`` (probe): the edges of that rule, over
  DD411..DD416 = {7, 1, 7, 7, 2, 7}: an old result one below the range (410),
  one above it (417) and negative (-5) each miss, -1 (adopted); a search that
  restarts from below the range reads 411 for 410 (and for -5); the first
  register (411, a match) resumes after it, 413.  (A reversed range is not
  probed: the exporter writes it as an address list, which a CLICK Search
  does not take.)
- ``dint_real_tables``: DD and DF ranges compare at full width (100000 does not
  fit an Int) and still return the register number.
- ``text_windows``: text search matches a window of characters, the result is
  the register of the window's first character, windows may end on the last
  character, and continuous mode resumes after the previous window start.

Each case's ``init`` clears its result registers and found bits (and the staged
tables); scan-paced cases count their own scans (``scans.py``).
"""

from __future__ import annotations

from pyrung.conformance import Group, assert_all_passed, case, run
from pyrung.core import Rung, copy, fill, latch, reset, search
from tests.conformance.memory import c, dd, df, ds, txt
from tests.conformance.scans import count_scans, done, scans_for

SCANS = scans_for()


def _stage(slot, base, values, *gate):
    """Write *values* to consecutive DS from *base*, while the gate holds."""
    with Rung(slot.Cmd != 0, *gate):
        for i, value in enumerate(values):
            copy(value, ds[base + i])


def _at(slot, scan, *more):
    return Rung(slot.Cmd != 0, slot.Scratch == scan, *more)


def _init(lo, hi, *bits):
    def init(slot):
        fill(0, ds.select(lo, hi))
        for n in bits:
            reset(c[n])

    return init


# --- hit, miss, operators, bounds ---------------------------------------------


def prove_hit_returns_address(slot):
    _stage(slot, 4203, [5, 20, 7, 20, 9], slot.Fired == 0)
    with Rung(slot.Cmd != 0, slot.Fired == 0):
        search(ds.select(4203, 4207) == 20, result=slot.Result1, found=c[851])
    with Rung(slot.Cmd != 0, slot.Fired == 0, c[851]):
        copy(1, slot.Result2)
    with Rung(slot.Cmd != 0, slot.Fired == 0):
        copy(1, slot.Fired)


def prove_miss_writes_minus_one(slot):
    _stage(slot, 4223, [1, 2, 3], slot.Fired == 0)
    with Rung(slot.Cmd != 0, slot.Fired == 0):
        copy(77, slot.Result1)
        latch(c[852])
    with Rung(slot.Cmd != 0, slot.Fired == 0):
        search(ds.select(4223, 4225) == 99, result=slot.Result1, found=c[852])
    with Rung(slot.Cmd != 0, slot.Fired == 0, ~c[852]):
        copy(1, slot.Result2)
    with Rung(slot.Cmd != 0, slot.Fired == 0):
        copy(1, slot.Fired)


def prove_operators_signed(slot):
    _stage(slot, 4243, [3, -8, 6, 9, -2], slot.Fired == 0)
    with Rung(slot.Cmd != 0, slot.Fired == 0):
        search(ds.select(4243, 4247) > 3, result=slot.Result1, found=c[853])
        search(ds.select(4243, 4247) < 0, result=slot.Result2, found=c[854])
        search(ds.select(4243, 4247) <= 3, result=slot.Result3, found=c[855])
        search(ds.select(4243, 4247) >= 9, result=slot.Result4, found=c[856])
        search(ds.select(4243, 4247) != 3, result=slot.Scratch, found=c[857])
    with Rung(slot.Cmd != 0, slot.Fired == 0):
        copy(1, slot.Fired)


def prove_range_ends_inclusive(slot):
    _stage(slot, 4263, [7, 1, 2, 9], slot.Fired == 0)
    with Rung(slot.Cmd != 0, slot.Fired == 0):
        search(ds.select(4263, 4266) == 7, result=slot.Result1, found=c[858])
        search(ds.select(4263, 4266) == 9, result=slot.Result2, found=c[859])
        search(ds.select(4263, 4265) == 9, result=slot.Result3, found=c[860])
        search(ds.select(4264, 4266) == 7, result=slot.Result4, found=c[861])
    with Rung(slot.Cmd != 0, slot.Fired == 0):
        copy(1, slot.Fired)


# --- a false rung -------------------------------------------------------------


def prove_false_rung_preserves(slot):
    # Search A (== 9, a hit) runs on scan 2 only, search B (== 50, a miss) on
    # scan 3 only, with found B on beforehand.  From scan 4 the table holds no 9
    # and a 50 at 4285, so a search that still ran would answer differently.
    count_scans(slot)
    _stage(slot, 4283, [4, 9, 4], slot.Scratch < 4)
    with Rung(slot.Cmd != 0, slot.Scratch >= 4):
        copy(0, ds[4284])
        copy(50, ds[4285])
    with _at(slot, 1):
        latch(c[863])
    with _at(slot, 2):
        search(ds.select(4283, 4285) == 9, result=ds[4291], found=c[862])
    with _at(slot, 3):
        search(ds.select(4283, 4285) == 50, result=ds[4292], found=c[863])
    with _at(slot, 8):
        copy(ds[4291], slot.Result1)
        copy(ds[4292], slot.Result3)
    with _at(slot, 8, c[862]):
        copy(1, slot.Result2)
    with _at(slot, 8, ~c[863]):
        copy(1, slot.Result4)
    done(slot)


# --- continuous ---------------------------------------------------------------


def _record(slot, reg, count=4):
    fields = (slot.Result1, slot.Result2, slot.Result3, slot.Result4)
    for n in range(1, count + 1):
        with _at(slot, n):
            copy(reg, fields[n - 1])


def prove_continuous_walks(slot):
    # Matches at 4303, 4305, 4306, 4308; the rung is true on scans 1..4.
    count_scans(slot)
    _stage(slot, 4303, [7, 1, 7, 7, 2, 7])
    with Rung(slot.Cmd != 0, slot.Scratch <= 4):
        search(ds.select(4303, 4308) == 7, result=ds[4311], found=c[864], continuous=True)
    _record(slot, ds[4311])
    done(slot)


def prove_continuous_exhausted(slot):
    # Matches at 4323, 4325.  Scan 1: 4323, scan 2: 4325, scan 3: miss, scan 4:
    # still a miss.  The ladder writes 0 to the result on scan 5, above the search.
    count_scans(slot)
    _stage(slot, 4323, [7, 1, 7, 2])
    with _at(slot, 5):
        copy(0, ds[4331])
    with Rung(slot.Cmd != 0, slot.Scratch <= 5):
        search(ds.select(4323, 4326) == 7, result=ds[4331], found=c[865], continuous=True)
    with _at(slot, 3):
        copy(ds[4331], slot.Result1)
    with _at(slot, 4):
        copy(ds[4331], slot.Result2)
    with _at(slot, 4, ~c[865]):
        copy(1, slot.Result4)
    with _at(slot, 5):
        copy(ds[4331], slot.Result3)
    done(slot)


def prove_continuous_zero_restarts(slot):
    # Matches at 4343, 4345, 4346, 4348.  The ladder writes 0 to the result on
    # scan 3, above the search.
    count_scans(slot)
    _stage(slot, 4343, [7, 1, 7, 7, 2, 7])
    with _at(slot, 3):
        copy(0, ds[4351])
    with Rung(slot.Cmd != 0, slot.Scratch <= 4):
        search(ds.select(4343, 4348) == 7, result=ds[4351], found=c[866], continuous=True)
    _record(slot, ds[4351])
    done(slot)


def prove_plain_search_ignores_result(slot):
    # Matches at 4363, 4365, 4366; the result is preset to 4365 on scan 1.
    count_scans(slot)
    _stage(slot, 4363, [7, 1, 7, 7, 2])
    with _at(slot, 1):
        copy(4365, ds[4371])
    with Rung(slot.Cmd != 0, slot.Scratch <= 3):
        search(ds.select(4363, 4367) == 7, result=ds[4371], found=c[867])
    _record(slot, ds[4371], 3)
    with _at(slot, 3, c[867]):
        copy(1, slot.Result4)
    done(slot)


def prove_continuous_cursor_is_address(slot):
    # Matches at 4383, 4385, 4386, 4388.  The results are preset, then searched
    # once each.
    _stage(slot, 4383, [7, 1, 7, 7, 2, 7], slot.Fired == 0)
    with Rung(slot.Cmd != 0, slot.Fired == 0):
        copy(4385, ds[4391])
        copy(4384, ds[4392])
        copy(4388, ds[4393])
        copy(5, ds[4394])
    with Rung(slot.Cmd != 0, slot.Fired == 0):
        search(ds.select(4383, 4388) == 7, result=ds[4391], found=c[868], continuous=True)
        search(ds.select(4383, 4388) == 7, result=ds[4392], found=c[869], continuous=True)
        search(ds.select(4383, 4388) == 7, result=ds[4393], found=c[870], continuous=True)
        search(ds.select(4383, 4388) == 7, result=ds[4394], found=c[871], continuous=True)
        copy(ds[4391], slot.Result1)
        copy(ds[4392], slot.Result2)
        copy(ds[4393], slot.Result3)
        copy(ds[4394], slot.Result4)
        copy(1, slot.Fired)


# --- other tables -------------------------------------------------------------


def prove_continuous_cursor_outside_range(slot):
    # DD411..DD416 = {7, 1, 7, 7, 2, 7}; each result preset, then searched once.
    with Rung(slot.Cmd != 0, slot.Fired == 0):
        for i, v in enumerate([7, 1, 7, 7, 2, 7]):
            copy(v, dd[411 + i])
        copy(410, slot.Result1)
        copy(417, slot.Result2)
        copy(-5, slot.Result3)
        copy(411, slot.Result4)
    with Rung(slot.Cmd != 0, slot.Fired == 0):
        search(dd.select(411, 416) == 7, result=slot.Result1, found=c[878], continuous=True)
        search(dd.select(411, 416) == 7, result=slot.Result2, found=c[879], continuous=True)
        search(dd.select(411, 416) == 7, result=slot.Result3, found=c[880], continuous=True)
        search(dd.select(411, 416) == 7, result=slot.Result4, found=c[881], continuous=True)
        copy(1, slot.Fired)


def init_continuous_cursor_outside_range(slot):
    fill(0, dd.select(411, 416))
    for n in (878, 879, 880, 881):
        reset(c[n])


def prove_dint_real_tables(slot):
    with Rung(slot.Cmd != 0, slot.Fired == 0):
        copy(10, dd[401])
        copy(100000, dd[402])
        copy(5, dd[403])
        copy(100000, dd[404])
        copy(1.5, df[301])
        copy(2.5, df[302])
        copy(3.5, df[303])
    with Rung(slot.Cmd != 0, slot.Fired == 0):
        search(dd.select(401, 404) >= 100000, result=slot.Result1, found=c[872])
        search(df.select(301, 303) > 2.0, result=slot.Result2, found=c[873])
        copy(1, slot.Fired)


def prove_text_windows(slot):
    # X A B Y A B Z A at TXT201..TXT208.
    with Rung(slot.Cmd != 0, slot.Fired == 0):
        copy("XABYABZA", txt[201])
    with Rung(slot.Cmd != 0, slot.Fired == 0):
        copy(202, slot.Result4)
        search(txt.select(201, 208) == "AB", result=slot.Result1, found=c[874])
        search(txt.select(201, 208) == "ZA", result=slot.Result2, found=c[875])
        search(txt.select(201, 208) == "AZ", result=slot.Result3, found=c[876])
        search(
            txt.select(201, 208) == "AB",
            result=slot.Result4,
            found=c[877],
            continuous=True,
        )
        copy(1, slot.Fired)


def init_dint_real_tables(slot):
    fill(0, dd.select(401, 404))
    fill(0.0, df.select(301, 303))
    for n in (872, 873):
        reset(c[n])


def init_text_windows(slot):
    for n in range(201, 209):
        copy("", txt[n])
    for n in (874, 875, 876, 877):
        reset(c[n])


group = Group(
    "search",
    [
        case(
            "A search of DS4203..DS4207 = {5, 20, 7, 20, 9} for == 20 writes the register number "
            "of the first match, 4204 (Result1), and turns found on (Result2 = 1)",
            ladder=prove_hit_returns_address,
            init=_init(4203, 4207, 851),
            expect={"Result1": 4204, "Result2": 1},
        ),
        case(
            "A search that finds nothing writes -1 over the old result and turns found off: "
            "result 77 and found on become -1 (Result1) and off (Result2 = 1 when found is off)",
            ladder=prove_miss_writes_minus_one,
            init=_init(4223, 4225, 852),
            expect={"Result1": -1, "Result2": 1},
        ),
        case(
            "Search compares signed values and the operators are exact over "
            "DS4243..DS4247 = {3, -8, 6, 9, -2}: > 3 finds 4245 (Result1), < 0 finds 4244 "
            "(Result2), <= 3 finds 4243 (Result3), >= 9 finds 4246 (Result4), != 3 finds "
            "4244 (Scratch)",
            ladder=prove_operators_signed,
            init=_init(4243, 4247, 853, 854, 855, 856, 857),
            expect={
                "Result1": 4245,
                "Result2": 4244,
                "Result3": 4243,
                "Result4": 4246,
                "Scratch": 4244,
            },
        ),
        case(
            "Both ends of a search range are included and nothing outside it matches: over "
            "DS4263..DS4266 = {7, 1, 2, 9}, == 7 finds 4263 (Result1) and == 9 finds 4266 "
            "(Result2); the same == 9 over DS4263..DS4265 (Result3) and == 7 over "
            "DS4264..DS4266 (Result4) find nothing, -1",
            ladder=prove_range_ends_inclusive,
            init=_init(4263, 4266, 858, 859, 860, 861),
            expect={"Result1": 4263, "Result2": 4266, "Result3": -1, "Result4": -1},
        ),
        case(
            "A search whose rung is false keeps its result and found, and does not search: a "
            "hit on scan 2 (4284, found on) and a miss on scan 3 (-1, found off) are the same "
            "on scan 8, though from scan 4 the table would now give a miss and a hit",
            ladder=prove_false_rung_preserves,
            init=_init(4283, 4292, 862, 863),
            expect={"Result1": 4284, "Result2": 1, "Result3": -1, "Result4": 1},
            scans=SCANS,
        ),
        case(
            "A continuous search with its rung true each scan finds the next match each scan: "
            "over DS4303..DS4308 = {7, 1, 7, 7, 2, 7} for == 7 the results on scans 1..4 are "
            "4303, 4305, 4306, 4308 (Result1..4)",
            ladder=prove_continuous_walks,
            init=_init(4303, 4311, 864),
            expect={"Result1": 4303, "Result2": 4305, "Result3": 4306, "Result4": 4308},
            scans=SCANS,
        ),
        case(
            "A continuous search past its last match gives -1 and keeps giving -1 (it does "
            "not wrap), found off; writing 0 to the result restarts it at the first match: "
            "over DS4323..DS4326 = {7, 1, 7, 2} the result is -1 on scan 3 (Result1) and "
            "scan 4 (Result2, Result4 = 1 when found is off), then 4323 on scan 5 (Result3) "
            "after the ladder wrote 0",
            ladder=prove_continuous_exhausted,
            init=_init(4323, 4331, 865),
            expect={"Result1": -1, "Result2": -1, "Result3": 4323, "Result4": 1},
            scans=SCANS,
        ),
        case(
            "Writing 0 to the result of a continuous search restarts it from the first "
            "address, also part way through: over DS4343..DS4348 = {7, 1, 7, 7, 2, 7} the "
            "results on scans 1..4 are 4343, 4345, 4343, 4345 when the ladder writes 0 on "
            "scan 3",
            ladder=prove_continuous_zero_restarts,
            init=_init(4343, 4351, 866),
            expect={"Result1": 4343, "Result2": 4345, "Result3": 4343, "Result4": 4345},
            scans=SCANS,
        ),
        case(
            "A search that is not continuous ignores the old result and starts at the first "
            "address every scan: over DS4363..DS4367 = {7, 1, 7, 7, 2} with the result preset "
            "to 4365 it still finds 4363 on scans 1..3 (Result1..3), found on (Result4 = 1)",
            ladder=prove_plain_search_ignores_result,
            init=_init(4363, 4371, 867),
            expect={"Result1": 4363, "Result2": 4363, "Result3": 4363, "Result4": 1},
            scans=SCANS,
        ),
        case(
            "A continuous search resumes at the first register after an old result inside "
            "its range, and misses from one outside it: over DS4383..DS4388 = {7, 1, 7, 7, 2, "
            "7} for == 7, an old result of 4385 (a match) gives 4386 (Result1), 4384 (not a "
            "match) gives 4385 (Result2), 4388 (the last register) gives -1 (Result3), and 5 "
            "(below the range) gives -1 (Result4)",
            ladder=prove_continuous_cursor_is_address,
            init=_init(4383, 4394, 868, 869, 870, 871),
            expect={"Result1": 4386, "Result2": 4385, "Result3": -1, "Result4": -1},
        ),
        case(
            "Search over DD and DF ranges compares at full width and returns register "
            "numbers: DD401..DD404 = {10, 100000, 5, 100000} for >= 100000 finds 402 "
            "(Result1), DF301..DF303 = {1.5, 2.5, 3.5} for > 2.0 finds 302 (Result2)",
            ladder=prove_dint_real_tables,
            init=init_dint_real_tables,
            expect={"Result1": 402, "Result2": 302},
        ),
        case(
            "A text search matches a window of characters and returns the register of the "
            'window\'s first character: TXT201..TXT208 = XABYABZA, "AB" finds 202 (Result1), '
            '"ZA" at the very end finds 207 (Result2), "AZ" finds nothing, -1 (Result3), and '
            'a continuous "AB" search with the result at 202 finds 205 (Result4)',
            ladder=prove_text_windows,
            init=init_text_windows,
            expect={"Result1": 202, "Result2": 207, "Result3": -1, "Result4": 205},
        ),
        case(
            "A continuous search misses from any old result outside its range: over "
            "DD411..DD416 = {7, 1, 7, 7, 2, 7} for == 7, an old result of 410 (just below), "
            "417 (just above) and -5 each give -1 (Result1..3), and 411 (the first register, "
            "a match) gives 413 (Result4)",
            ladder=prove_continuous_cursor_outside_range,
            init=init_continuous_cursor_outside_range,
            expect={"Result1": -1, "Result2": -1, "Result3": -1, "Result4": 413},
            probe="continuous search cursor at the range edges (410, 417, -5)",
        ),
    ],
)


def test_search():
    assert_all_passed(run(group))
