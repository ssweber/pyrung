"""Program-step estimate for an exported CLICK ladder.

CLICK caps a project at 8000 program steps and does not document what each
element costs.  The costs here were measured on CLICK Programming Software
(2026-10-29): a probe project with one element per rung, deleted rung by rung,
the step count read after each deletion.  The estimate walks the exported CSV
rows, so it sees exactly what CLICK is given.

Measured:

- A contact (NO, NC, edge, immediate) is 1; a compare contact is 3.
- A rung with no contact on its first row pays 1 for the always-on rail.
  Rungs have no other overhead, and comments cost nothing.
- OR'd contacts cost only themselves.  A rung with n >= 2 output legs pays
  ``1 + (legs without a contact of their own)`` on top of its instructions.
- Pin rows (``.reset()``, ``.down()``, ``.clock()``) cost their contacts.
- Main with its END is 3; each subroutine with its closing RET is 4.
- For and Next rungs together are 5 with no contacts (``for`` 2, ``next`` 1,
  a rail each); ``break`` and ``continue`` are 1.

Fits with too few points to be exact, rounded up: math (``2 + operands +
operators``, one over on a formula with REAL constants), drums (one 2x3 drum
measured at 14; scaling with pattern size is a guess), the split of For/Next's
3 between For and Next, and legs whose first leg has a contact of its own.
Checked against a whole project: conformance part A (layout 28574) estimated
6067 with break/continue unmeasured, CLICK reported 6088.
An instruction never measured costs :data:`UNMEASURED_COST` and is listed in
:attr:`StepEstimate.unmeasured`.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pyrung.click.ladder.types import LadderBundle

#: CLICK's ladder program limit.
CLICK_STEP_LIMIT = 8000

MAIN_BASE = 3
SUBROUTINE_BASE = 4
RAIL = 1
CONTACT = 1
COMPARE_CONTACT = 3
#: Cost of an instruction no probe has measured: high on purpose.
UNMEASURED_COST = 8

#: Measured cost of each AF token, by function name, contacts excluded.
INSTRUCTION_COSTS: dict[str, int] = {
    "out": 1,
    "latch": 1,
    "reset": 1,
    "toggle": 1,
    "flasher": 4,
    "on_delay": 2,
    "off_delay": 2,
    "count_up": 2,
    "count_down": 2,
    "copy": 4,
    "blockcopy": 5,
    "fill": 4,
    "pack_bits": 5,
    "unpack_to_bits": 4,
    "pack_words": 5,
    "unpack_to_words": 4,
    "search": 6,
    "shift": 2,
    "call": 1,
    "return": 1,
    "for": 2,
    "next": 1,
    "break": 1,
    "continue": 1,
}

_WIRES = frozenset({"", "-", "T", "|"})
_COMPARE = re.compile(r"==|!=|<=|>=|<|>")
_FORMULA_OPERAND = re.compile(r"^(?:[A-Z]+\d+(?::[A-Z]+\d+)?|\d+(?:\.\d+)?)$")
_FORMULA_TOKEN = re.compile(r"[A-Za-z_]+\d*(?::[A-Z]+\d+)?|\d+(?:\.\d+)?|\*\*|[^\s(),]")


@dataclass(frozen=True)
class StepEstimate:
    """Estimated CLICK program steps of one exported project."""

    main: int
    subroutines: dict[str, int] = field(default_factory=dict)
    #: Function names costed at :data:`UNMEASURED_COST`, sorted.
    unmeasured: tuple[str, ...] = ()

    @property
    def total(self) -> int:
        return self.main + sum(self.subroutines.values())

    def fits(self, limit: int = CLICK_STEP_LIMIT) -> bool:
        return self.total <= limit


def estimate_steps(bundle: LadderBundle) -> StepEstimate:
    """Estimate the CLICK program steps of *bundle* (from ``pyrung_to_ladder``)."""
    unmeasured: set[str] = set()
    main = MAIN_BASE + _rows_cost(bundle.main_rows, unmeasured)
    subs = {
        name: SUBROUTINE_BASE + _rows_cost(rows, unmeasured)
        for name, rows in bundle.subroutine_rows
    }
    return StepEstimate(main=main, subroutines=subs, unmeasured=tuple(sorted(unmeasured)))


def _rows_cost(rows: Sequence[Sequence[str]], unmeasured: set[str]) -> int:
    rungs = list(_rungs(rows[1:]))  # Skip the header.
    if rungs and _is_free_tail(rungs[-1]):
        rungs.pop()
    return sum(_rung_cost(rung, unmeasured) for rung in rungs)


def _rungs(rows: Iterable[Sequence[str]]) -> Iterable[list[Sequence[str]]]:
    rung: list[Sequence[str]] = []
    for row in rows:
        marker = row[0]
        if marker.startswith("#"):
            continue
        if marker.startswith("R") and rung:
            yield rung
            rung = []
        rung.append(row)
    if rung:
        yield rung


def _is_free_tail(rung: list[Sequence[str]]) -> bool:
    """END and a closing unconditional RET are in the base costs."""
    if len(rung) != 1:
        return False
    row = rung[0]
    return row[-1] in ("end()", "return()") and all(cell == "-" for cell in row[1:-1])


def _contacts(row: Sequence[str]) -> list[str]:
    found = []
    for cell in row[1:-1]:
        if cell in _WIRES:
            continue
        found.append(cell[2:] if cell.startswith("T:") else cell)
    return found


def _rung_cost(rung: list[Sequence[str]], unmeasured: set[str]) -> int:
    cost = 0
    if not _contacts(rung[0]):
        cost += RAIL
    legs = 0
    legs_without_contact = 0
    for index, row in enumerate(rung):
        contacts = _contacts(row)
        cost += sum(COMPARE_CONTACT if _COMPARE.search(c) else CONTACT for c in contacts)
        token = row[-1]
        if not token or token.startswith("."):
            continue
        cost += _instruction_cost(token, unmeasured)
        legs += 1
        if index == 0 or not contacts:
            legs_without_contact += 1
    if legs >= 2:
        cost += 1 + legs_without_contact
    return cost


def _instruction_cost(token: str, unmeasured: set[str]) -> int:
    name = token.split("(", 1)[0]
    if name == "reset" and ".." in token:
        return 2
    if name == "math":
        return _math_cost(token)
    if name in ("event_drum", "time_drum", "time_event_drum"):
        return _drum_cost(token)
    cost = INSTRUCTION_COSTS.get(name)
    if cost is None:
        unmeasured.add(name)
        return UNMEASURED_COST
    return cost


def _math_cost(token: str) -> int:
    formula = token[len("math(") :].split(",", 1)[0]
    tokens = _FORMULA_TOKEN.findall(formula)
    operands = sum(1 for t in tokens if _FORMULA_OPERAND.match(t))
    return 2 + operands + (len(tokens) - operands)


def _drum_cost(token: str) -> int:
    # One 2 outputs x 3 steps drum measured 14 (event and time alike).
    pattern = re.search(r"pattern=\[(.*?)\]\]", token)
    cells = pattern.group(1).count(",") + 1 if pattern else 6
    return 8 + cells
