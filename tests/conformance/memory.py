"""Raw CLICK memory the conformance groups own, and Timer/Counter handles on it.

A slot holds a case's command and results (DS, from the manifest).  Anything
else a case touches -- C bits, timers, counters, staged DS/DD/DF/DH, TXT -- is a
raw CLICK address, and nothing allocates those: a case picks addresses inside
its group's ranges in :data:`OWNED`.  A new group gets its ranges here before
its first case.  ``test_build`` checks that every case stays inside its group's
ranges, that the ranges are disjoint and inside CLICK's banks, and that no
manifest block reaches :data:`STAGING_DS`.

Banks are keyed by CLICK's prefix.  ``T`` is the timer number (``T<n>`` and
``TD<n>``), ``CT`` the counter number (``CT<n>`` and ``CTD<n>``).

System points (SC/SD) are not raw memory: read them through pyrung's
``system`` namespace (``system.fault.out_of_range`` is SC43,
``system.sys.mode_run`` SC11), which the runtime drives and the exporter maps
to the CLICK address.  A raw ``sc[43]`` would be a plain tag nothing writes
(it reads 0 in-process and on the sim), so this module does not export one
and ``outside_owned`` reports a case that uses one.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass

from pyrung.click import ClickBlocks
from pyrung.conformance import Group
from pyrung.conformance._ladder import footprint
from pyrung.core import Tag

_blocks = ClickBlocks()
c, t, td, ct, ctd = _blocks.c, _blocks.t, _blocks.td, _blocks.ct, _blocks.ctd
ds, dd, df, dh, txt = _blocks.ds, _blocks.dd, _blocks.df, _blocks.dh, _blocks.txt

#: Size of each bank a group can own (addresses 1..N).
BANKS: Mapping[str, int] = {
    "C": 2000,
    "T": 500,
    "CT": 250,
    "DS": 4500,
    "DD": 1000,
    "DF": 500,
    "DH": 500,
    "TXT": 1000,
}

#: Raw DS a case stages in (a contiguous source range, a search table, ...)
#: lives in this area.  No manifest block may overlap a DS range a group owns;
#: the registers inside it that no group's cases use are free for the S6 probes'
#: blocks (``drum_idle``, ``drum_rule``, ``store_probe``, ``drum_jump``);
#: ``drum_settle``'s block sits on the slots ``math`` never used, DS391..DS500,
#: and ``drum_close``'s on those ``rungs`` never used, DS221..DS300.
STAGING_DS = range(3901, 4501)

#: Raw memory each group owns, by bank: a range, or a tuple of ranges for a
#: group that outgrew its first one.  A group listed before it has cases is
#: reserved for the group that will have them.
OWNED: Mapping[str, Mapping[str, range | tuple[range, ...]]] = {
    "rungs": {
        "C": (
            range(1601, 1603),
            range(1611, 1614),
            range(1621, 1623),
            range(1631, 1633),
            range(1634, 1661),
        ),
        "CT": range(151, 161),
    },
    # DD4.., DF20.. and DH5.. are the grey-area probes' (the math cases use none).
    "math": {
        "DS": (
            range(4401, 4403),
            range(4411, 4413),
            range(4421, 4424),
            range(4431, 4439),
        ),
        "DD": range(1, 4),
        "DF": range(1, 20),
        "DH": range(1, 5),
    },
    "drum": {"C": (range(1901, 2001), range(801, 851)), "T": range(401, 501)},
    # S6 probes (tests/conformance/drum_shift_search/test_drum_rule.py, test_drum_jump.py,
    # test_drum_idle.py, test_store_probe.py).  Their C, T, DD and DS come from
    # what the groups around them leave unused.
    "drum_rule": {
        "C": (range(101, 201), range(771, 801)),
        "T": range(161, 181),
        "DD": range(641, 661),
        "DS": range(4191, 4202),
    },
    "drum_jump": {
        "C": (range(1110, 1201), range(1401, 1471)),
        "T": range(251, 271),
        "DD": range(661, 681),
        "DS": (range(3993, 4001), range(4202, 4203), range(4395, 4399), range(4500, 4501)),
    },
    "drum_idle": {
        "C": range(1751, 1781),
        "T": range(351, 359),
        "DD": range(681, 689),
        "DS": range(4399, 4401),
    },
    "drum_settle": {
        "C": (range(1852, 1901), range(582, 661)),
        "T": range(359, 377),
        "DD": range(718, 754),
        "DS": (range(4208, 4223), range(4227, 4231)),
    },
    # What drum_settle left for the fix (tests/conformance/drum_shift_search/test_drum_close.py).
    "drum_close": {
        "C": (range(702, 751), range(969, 1001), range(414, 451), range(27, 51)),
        "T": (range(271, 299), range(377, 401)),
        "DD": range(754, 810),
        "DS": (range(4332, 4343), range(4352, 4363), range(4372, 4383)),
    },
    "store_probe": {
        "C": (range(1661, 1701), range(1781, 1801)),
        "DD": range(611, 641),
        "DF": range(421, 430),
        "DH": range(411, 417),
        "TXT": range(521, 546),
        "DS": range(4499, 4500),
    },
    # The fault-flag model's unmeasured choices (tests/conformance/system/test_flag_probe.py,
    # test_past_bank_write.py); their memory is what the counters group and the free
    # tails of the DD, DF, DH and TXT banks leave.
    "flag_probe": {
        "C": range(1331, 1381),
        "T": range(181, 182),
        "CT": range(147, 148),
        "DD": range(689, 718),
        "DF": range(430, 434),
        "DH": range(417, 422),
        "TXT": range(501, 503),
    },
    "past_write": {"C": range(1381, 1383), "DS": range(4226, 4227)},
    # A drum's state scan by scan (tests/conformance/drum_shift_search/test_drum_trace.py).
    "drum_trace": {"C": range(51, 101)},
    # C1852..C1900 are drum_settle's (the out cases stop at C1851).
    "out": {"C": range(1801, 1852), "T": range(301, 313)},
    "loop": {
        "C": (
            range(1701, 1702),
            range(1711, 1712),
            range(1720, 1722),
            range(1729, 1732),
            range(1741, 1751),
        ),
        "T": range(201, 212),
        "CT": (range(201, 212), range(215, 251)),
    },
    "timers": {"C": (range(1471, 1472), range(1481, 1482), range(1491, 1492)), "T": range(1, 101)},
    # C1331..C1380, C1381..C1382 and CT147 are the fault-flag probes' (the counters
    # cases use none of them).
    "counters": {"C": (range(1301, 1302), range(1383, 1401)), "CT": range(1, 147)},
    "coils": {
        "C": (
            range(1501, 1502),
            range(1511, 1512),
            range(1521, 1522),
            range(1531, 1532),
            range(1541, 1543),
            range(1551, 1552),
            range(1561, 1562),
            range(1571, 1573),
            range(1581, 1583),
            range(1591, 1593),
        )
    },
    "numeric": {
        "DD": (range(101, 133), range(161, 164), range(171, 173)),
        "DF": range(101, 142),
        "DH": range(201, 223),
    },
    "text": {
        "TXT": (range(1, 201), range(301, 401)),
        "DS": (range(3901, 3907), range(3911, 3915), range(3921, 3924), range(3931, 3933)),
        "DD": range(301, 401),
        "DH": range(301, 401),
    },
    "block": {
        "C": range(1201, 1277),
        "DS": (range(4001, 4016), range(4021, 4111)),
        "DD": range(201, 301),
        "DF": (range(201, 203), range(205, 301)),
        "DH": range(101, 201),
    },
    "shift": {
        "C": (
            range(1001, 1030),
            range(1041, 1050),
            range(1061, 1070),
            range(1081, 1090),
            range(1101, 1110),
        )
    },
    "search": {
        "C": range(851, 882),
        # DS4226: past_write's pointer; DS4208..DS4222, DS4227..DS4230: drum_settle's;
        # DS4332..DS4342, DS4352..DS4362, DS4372..DS4382: drum_close's.
        "DS": (
            range(4203, 4208),
            range(4223, 4226),
            range(4243, 4248),
            range(4263, 4267),
            range(4283, 4293),
            range(4303, 4312),
            range(4323, 4332),
            range(4343, 4352),
            range(4363, 4372),
            range(4383, 4395),
        ),
        "DD": range(401, 501),
        "DF": range(301, 401),
        "TXT": range(201, 301),
    },
    "system": {
        # C969..C1000 are drum_close's.
        "C": (range(901, 904), range(911, 914), range(921, 924), range(941, 969)),
        "T": (range(101, 113), range(126, 151)),
        "CT": range(171, 181),
        "DD": range(901, 1000),
        "DH": range(401, 411),
    },
    # Math errors (tests/conformance/math/test_math_error.py).
    "math_error": {"DF": range(401, 411)},
    # A subroutine called twice a scan (tests/conformance/program_control/test_call_twice.py).
    # C702..C750 are drum_close's.
    "call_twice": {"C": range(701, 702)},
    # Solve order within a rung (tests/conformance/rungs/test_solve_order.py).
    # C582..C660 are drum_settle's (the solve_order cases use none of them).
    "solve_order": {
        "C": (range(501, 503), range(511, 522), range(527, 582), range(661, 662), range(681, 682)),
        "T": range(151, 161),
        "CT": range(161, 171),
    },
    # Row solve, round 3 (tests/conformance/rungs/test_row_solve.py).
    # C414..C450 and C27..C50 are drum_close's.
    "row_solve": {"C": (range(401, 414), range(1, 27)), "CT": range(191, 196)},
    # A box on a continued wire (tests/conformance/rungs/test_cont_box.py).
    "cont_box": {"C": range(451, 501), "CT": range(181, 191)},
    # Which stores raise a fault flag (tests/conformance/system/test_faults.py).
    "faults": {
        "C": range(201, 401),
        "DD": (range(501, 601), range(1000, 1001)),
        "DF": range(451, 496),
        "TXT": range(401, 501),
    },
    # A FOR whose count is a Real (tests/conformance/program_control/test_for_count.py).
    "for_count": {"DD": range(601, 611), "DF": range(496, 501)},
    # Probes a CLICK may refuse at paste (tests/conformance/rungs/test_paste_risk.py).
    "paste_risk": {
        "C": range(751, 771),
        "CT": range(196, 201),
        "DF": range(411, 421),
    },
    # The grey-area probes (tests/conformance/probe_kit.py).  Their memory is
    # what the groups above leave unused.
    "word_store": {
        "DD": range(810, 850),
        "DF": range(434, 450),
        "DH": range(422, 470),
        "DS": (range(4312, 4316), range(4322, 4323)),
    },
    "real_bits": {
        "DD": range(850, 860),
        "DF": range(20, 70),
        "DH": range(470, 500),
        "TXT": range(546, 570),
    },
    # tests/conformance/drum_shift_search/test_drum_pins.py: from what other groups' cases never used.
    "drum_pins": {
        "C": (
            range(662, 681),
            range(682, 701),
            range(882, 901),
            range(904, 911),
            range(914, 921),
            range(924, 941),
            range(1030, 1041),
            range(1050, 1055),
        ),
        "T": (
            range(182, 201),
            range(313, 318),
        ),
        "DS": (
            range(4231, 4243),
            range(4248, 4260),
            range(4293, 4298),
        ),
        "DD": range(860, 884),
    },
    "drum_branch": {
        "C": (
            range(1055, 1061),
            range(1070, 1081),
            range(1090, 1101),
            range(1277, 1301),
            range(1302, 1331),
            range(1472, 1481),
            range(1482, 1491),
            range(1492, 1501),
            range(1502, 1511),
            range(1512, 1521),
            range(1522, 1531),
            range(1532, 1541),
            range(1543, 1548),
        ),
        "T": (
            range(212, 213),
            range(223, 224),
            range(318, 351),
        ),
        "DS": (
            range(3907, 3908),
            range(4260, 4263),
            range(4267, 4283),
            range(4298, 4303),
            range(4403, 4411),
            range(4413, 4420),
        ),
        "DD": (
            range(133, 150),
            range(884, 901),
        ),
    },
    "drum_limits": {
        "C": (
            range(1548, 1551),
            range(1552, 1561),
            range(1562, 1571),
            range(1573, 1581),
            range(1583, 1586),
        ),
        "T": (
            range(213, 223),
            range(224, 225),
        ),
        "DS": (
            range(3908, 3911),
            range(4316, 4318),
            range(4420, 4421),
            range(4424, 4431),
        ),
        "DD": range(150, 160),
    },
    "math_more": {
        "C": range(503, 511),
        "DD": range(4, 26),
        "DF": range(151, 170),
    },
    "hex_math": {"DH": range(5, 34)},
    "agg_more": {
        "C": (range(522, 527), range(1586, 1591)),
        "DS": range(4318, 4322),
        "DD": range(41, 57),
        "DF": range(170, 192),
        "DH": range(61, 67),
    },
    "real_sum": {"DF": (range(70, 101), range(142, 151))},
    "copy_more": {
        "C": range(1737, 1738),
        "DD": range(59, 61),
        "DF": range(192, 200),
        "DH": range(67, 70),
        "TXT": (range(570, 571), range(571, 751)),
    },
    "block_more": {
        "C": range(1738, 1741),
        "DS": range(3915, 3920),
        "DF": range(200, 201),
        "TXT": range(751, 754),
    },
    "coil_range": {
        "C": (range(1623, 1631), range(1633, 1634)),
        "DD": range(77, 78),
        "DH": range(72, 77),
    },
    "compare": {
        "DS": (range(3920, 3921), range(3924, 3926)),
        "DH": range(70, 71),
        "TXT": range(771, 777),
    },
    "out_more": {
        "C": (range(1702, 1711), range(1712, 1720)),
        "DS": range(3926, 3929),
        "T": (range(113, 116), range(225, 243)),
    },
    "for_more": {
        "C": range(1722, 1729),
        "DS": range(3929, 3931),
        "DD": range(78, 84),
        "T": (range(122, 123), range(244, 249)),
        "CT": range(149, 150),
    },
    "call_more": {
        "C": range(1732, 1737),
        "DS": range(4016, 4017),
        "T": (range(243, 244), range(249, 251)),
        "CT": (range(148, 149), range(150, 151)),
    },
    "boot_read": {
        "C": (range(1603, 1607), range(1609, 1611), range(1614, 1621)),
        "DS": (range(4017, 4018), range(4019, 4021)),
        "DD": range(65, 74),
        "DF": range(203, 204),
        "DH": range(71, 72),
        "T": range(116, 122),
        "CT": range(213, 215),
        "TXT": range(781, 782),
    },
    "for_wide": {
        "DD": range(61, 65),
        "T": (range(123, 124), range(125, 126)),
        "CT": range(212, 213),
    },
    "flag_scan": {"DF": range(204, 205)},
    "rtc_set": {"DD": (range(74, 77), range(84, 86))},
}


#: Raw memory of the groups in parts with memory of their own (``suite.APART_PARTS``):
#: they share nothing with :data:`OWNED`, which has no room left, and run in no project
#: with its groups.
OWNED_APART: Mapping[str, Mapping[str, range | tuple[range, ...]]] = {
    "unit_pairs": {"T": range(1, 11)},
    "unit_minute": {"T": range(11, 21)},
    "fixed_scan": {"T": range(21, 41), "C": range(1, 11), "DS": range(3901, 3903)},
    # On no address of OWNED: its never written TXT must be unwritten by any part's project.
    "text_compare": {"TXT": range(801, 851)},
    "flasher_td": {"T": range(41, 51), "C": range(11, 21), "DS": range(3903, 3905)},
    "flag_boxes": {
        "C": range(101, 116),
        "T": range(101, 103),
        "CT": range(1, 3),
        "DD": range(1, 9),
        "DS": range(3905, 3911),
    },
    "flag_halves": {
        "C": range(141, 174),
        "DD": range(21, 38),
        "DH": range(1, 6),
        "DF": range(1, 2),
        "TXT": range(1, 3),
        "DS": range(3911, 3914),
    },
    "math_terms": {"DD": (range(51, 56), range(61, 65)), "DF": range(11, 16), "C": range(181, 186)},
    "search_more": {
        "C": range(201, 205),
        "DH": range(11, 15),
        "DD": range(41, 43),
        "DS": range(3916, 3918),
    },
    "bad_jump": {
        "C": range(301, 322),
        "DS": range(3918, 3923),
        "T": range(201, 205),
        "DD": range(71, 75),
    },
    "drum_preset": {
        "C": (range(331, 337), range(341, 353)),
        "DS": range(3923, 3931),
        "T": range(205, 210),
    },
    "real_text": {"DF": range(21, 31), "TXT": range(101, 301)},
    "deb_zero": {"C": range(221, 223), "T": range(111, 115)},
    "coil_timing": {"C": range(241, 247), "T": range(115, 125)},
    # DS4226 is past_write's pointer, on its choices.
    "past_clean": {"C": range(231, 233), "DS": range(4226, 4227)},
    "for_dd_minus": {"C": range(401, 403), "DD": range(81, 83)},
    "word_floor": {"DD": range(91, 93), "DF": range(31, 33), "DH": range(21, 24)},
}


def ranges(owned: range | tuple[range, ...]) -> tuple[range, ...]:
    """An :data:`OWNED` entry as a tuple of ranges."""
    return owned if isinstance(owned, tuple) else (owned,)


#: Tag-name prefix -> bank key.
_PREFIX_BANK = {"TD": "T", "CTD": "CT"}
_ADDRESS = re.compile(r"([A-Z]+)(\d+)")


@dataclass(frozen=True)
class ClickTimer:
    """Timer ``n`` as CLICK addresses it: its done bit T<n> and accumulator TD<n>."""

    n: int

    @property
    def Done(self) -> Tag:
        return t[self.n]

    @property
    def Acc(self) -> Tag:
        return td[self.n]


@dataclass(frozen=True)
class ClickCounter:
    """Counter ``n`` as CLICK addresses it: its done bit CT<n> and accumulator CTD<n>."""

    n: int

    @property
    def Done(self) -> Tag:
        return ct[self.n]

    @property
    def Acc(self) -> Tag:
        return ctd[self.n]


def _address(name: str) -> tuple[str, int] | None:
    match = _ADDRESS.fullmatch(name)
    if match is None:
        return None
    prefix, n = match.group(1), int(match.group(2))
    return _PREFIX_BANK.get(prefix, prefix), n


def outside_owned(
    groups: Iterable[Group],
    table: Mapping[str, Mapping[str, range | tuple[range, ...]]] = OWNED,
) -> list[str]:
    """One line per tag a case touches outside its group's ranges in *table*."""
    problems: list[str] = []
    for g in groups:
        owned = table.get(g.name, {})
        for case in g.cases:
            everything, _writes = footprint(case)
            for name in sorted(everything):
                address = _address(name)
                if address is None:
                    problems.append(f"{g.name}/{case.id}: {name} is not a raw CLICK address")
                    continue
                bank, n = address
                if not any(n in r for r in ranges(owned.get(bank, ()))):
                    listed = ", ".join(
                        f"{b}{r.start}..{b}{r.stop - 1}"
                        for b, o in owned.items()
                        for r in ranges(o)
                    )
                    problems.append(
                        f"{g.name}/{case.id}: {name} is outside the group's memory "
                        f"({listed or 'none'}; see tests/conformance/memory.py)"
                    )
    return problems
