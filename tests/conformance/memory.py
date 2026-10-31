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

#: Raw DS a case stages in (a contiguous source range, a search table, ...).
#: No manifest block may reach it.
STAGING_DS = range(3901, 4501)

#: Raw memory each group owns, by bank: a range, or a tuple of ranges for a
#: group that outgrew its first one.  A group listed before it has cases is
#: reserved for the group that will have them.
OWNED: Mapping[str, Mapping[str, range | tuple[range, ...]]] = {
    "rungs": {"C": range(1601, 1701), "CT": range(151, 161)},
    "math": {
        "DS": range(4401, 4501),
        "DD": range(1, 101),
        "DF": range(1, 101),
        "DH": range(1, 101),
    },
    "drum": {"C": (range(1901, 2001), range(801, 851)), "T": range(401, 501)},
    "out": {"C": range(1801, 1901), "T": range(301, 401)},
    "loop": {"C": range(1701, 1801), "T": range(201, 301), "CT": range(201, 251)},
    "timers": {"C": range(1401, 1501), "T": range(1, 101)},
    "counters": {"C": range(1301, 1401), "CT": range(1, 151)},
    "coils": {"C": range(1501, 1601)},
    "numeric": {"DD": range(101, 201), "DF": range(101, 201), "DH": range(201, 301)},
    "text": {
        "TXT": (range(1, 201), range(301, 401)),
        "DS": range(3901, 4001),
        "DD": range(301, 401),
        "DH": range(301, 401),
    },
    "block": {
        "C": range(1201, 1301),
        "DS": range(4001, 4201),
        "DD": range(201, 301),
        "DF": range(201, 301),
        "DH": range(101, 201),
    },
    "shift": {"C": range(1001, 1201)},
    "search": {
        "C": range(851, 901),
        "DS": range(4201, 4401),
        "DD": range(401, 501),
        "DF": range(301, 401),
        "TXT": range(201, 301),
    },
    "system": {
        "C": range(901, 1001),
        "T": range(101, 151),
        "CT": range(171, 181),
        "DD": range(901, 1001),
        "DH": range(401, 411),
    },
    # Math errors (tests/conformance/test_math_error.py).
    "math_error": {"DF": range(401, 411)},
    # A subroutine called twice a scan (tests/conformance/test_call_twice.py).
    "call_twice": {"C": range(701, 751)},
    # Solve order within a rung (tests/conformance/test_solve_order.py).
    "solve_order": {"C": range(501, 701), "T": range(151, 161), "CT": range(161, 171)},
    # Row solve, round 3 (tests/conformance/test_row_solve.py).
    "row_solve": {"C": range(401, 451), "CT": range(191, 201)},
    # A box on a continued wire (tests/conformance/test_cont_box.py).
    "cont_box": {"C": range(451, 501), "CT": range(181, 191)},
    # Which stores raise a fault flag (tests/conformance/test_faults.py).
    "faults": {
        "C": range(201, 401),
        "DD": range(501, 601),
        "DF": range(451, 496),
        "TXT": range(401, 501),
    },
    # A FOR whose count is a Real (tests/conformance/test_for_count.py).
    "for_count": {"DD": range(601, 611), "DF": range(496, 501)},
    # Probes a CLICK may refuse at paste (tests/conformance/test_paste_risk.py).
    "paste_risk": {"C": range(751, 771), "DF": range(411, 421)},
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


def outside_owned(groups: Iterable[Group]) -> list[str]:
    """One line per tag a case touches outside its group's :data:`OWNED` ranges."""
    problems: list[str] = []
    for g in groups:
        owned = OWNED.get(g.name, {})
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
