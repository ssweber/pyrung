"""Raw CLICK memory the conformance groups own, and Timer/Counter handles on it.

A slot holds a case's command and results (DS, from the manifest).  Anything
else a case touches -- C bits, timers, counters, staged DS/DD/DF/DH, TXT -- is a
raw CLICK address, and nothing allocates those: a case picks addresses inside
its group's ranges in :data:`OWNED`.  A new group gets its ranges here before
its first case.  ``test_build`` checks that every case stays inside its group's
ranges, that the ranges are disjoint and inside CLICK's banks, and that no
manifest block reaches :data:`STAGING_DS`.

Banks are keyed by CLICK's prefix.  ``T`` is the timer number (``T<n>`` and
``TD<n>``), ``CT`` the counter number (``CT<n>`` and ``CTD<n>``).  SC and SD are
nobody's: any case may read them.
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
sc, sd = _blocks.sc, _blocks.sd

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

#: Raw memory each group owns, by bank.  A group listed before it has cases is
#: reserved for the group that will have them.
OWNED: Mapping[str, Mapping[str, range]] = {
    "rungs": {"C": range(1601, 1701)},
    "math": {
        "DS": range(4401, 4501),
        "DD": range(1, 101),
        "DF": range(1, 101),
        "DH": range(1, 101),
    },
    "drum": {"C": range(1901, 2001), "T": range(401, 501)},
    "out": {"C": range(1801, 1901), "T": range(301, 401)},
    "loop": {"C": range(1701, 1801), "T": range(201, 301), "CT": range(201, 251)},
    # Reserved for groups not written yet.
    "timers": {"C": range(1401, 1501), "T": range(1, 101)},
    "counters": {"C": range(1301, 1401), "CT": range(1, 101)},
    "coils": {"C": range(1501, 1601)},
    "numeric": {"DD": range(101, 201), "DF": range(101, 201)},
    "text": {"TXT": range(1, 201), "DS": range(3901, 4001)},
    "block": {
        "C": range(1201, 1301),
        "DS": range(4001, 4201),
        "DD": range(201, 301),
        "DH": range(101, 201),
    },
    "shift": {"C": range(1001, 1201)},
    "search": {"DS": range(4201, 4401)},
    "system": {"C": range(901, 1001), "T": range(101, 151)},
}

#: Prefixes any case may read without owning them.
_READ_ONLY = frozenset({"SC", "SD"})
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
            everything, writes = footprint(case)
            for name in sorted(everything):
                address = _address(name)
                if address is None:
                    problems.append(f"{g.name}/{case.id}: {name} is not a raw CLICK address")
                    continue
                bank, n = address
                if bank in _READ_ONLY and name not in writes:
                    continue
                if n not in owned.get(bank, ()):
                    ranges = ", ".join(f"{b}{r.start}..{b}{r.stop - 1}" for b, r in owned.items())
                    problems.append(
                        f"{g.name}/{case.id}: {name} is outside the group's memory "
                        f"({ranges or 'none'}; see tests/conformance/memory.py)"
                    )
    return problems
