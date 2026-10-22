"""The canonical conformance program: cases on fixed DS slots plus a build-hash header."""

from __future__ import annotations

import hashlib
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from pyrung.conformance._case import Case
from pyrung.conformance._slot import (
    COUNT_REGISTER,
    HASH_REGISTER,
    MAX_SLOTS,
    SLOT_BASE,
    SLOT_STRIDE,
    make_slot,
)
from pyrung.core import Int, Program, Rung, copy
from pyrung.core.system_points import system

if TYPE_CHECKING:
    from pyrung.click import TagMap
    from pyrung.click.ladder import LadderBundle
    from pyrung.core.structure import _NamedArrayRuntime


@dataclass(frozen=True)
class ConformanceProgram:
    """The program a Modbus run talks to, and the layout it was built with.

    ``cases`` are the cases that run over the wire, in slot order (slot 1 is
    ``cases[0]``).  ``skipped`` are the in-process-only cases left out of the
    program.  ``build_hash`` is the value the program writes to ``DS1`` on its
    first scan; the harness reads it before anything else.
    """

    cases: tuple[Case, ...]
    skipped: tuple[Case, ...]
    program: Program
    tag_map: TagMap
    slot: _NamedArrayRuntime
    build_hash: int

    @property
    def slot_count(self) -> int:
        return len(self.cases)

    @property
    def last_register(self) -> int:
        """Last DS register the slots occupy."""
        return SLOT_BASE + self.slot_count * SLOT_STRIDE - 1

    def export(self, directory: str | Path) -> Path:
        """Write the CLICK ladder CSVs and nickname CSV for loading on a real PLC.

        Returns the directory.  Load both into the CLICK software, then point
        the Modbus runner at the PLC.
        """
        from pyrung.click.ladder import pyrung_to_ladder

        out = Path(directory)
        out.mkdir(parents=True, exist_ok=True)
        pyrung_to_ladder(self.program, self.tag_map).write(out)
        self.tag_map.to_nickname_file(out / "nicknames.csv")
        return out


def build_program(cases: Sequence[Case]) -> ConformanceProgram:
    """Build the canonical program for *cases* (in-process-only cases are left out).

    Slot N lives at ``DS(10*N + 1)`` .. ``DS(10*N + 8)``.  The program's first
    rung writes the build hash to ``DS1`` and the slot count to ``DS2`` on the
    first scan.  The hash covers the CLICK export of the case rungs, so it
    changes whenever the exported ladder would.
    """
    from pyrung.click import ClickBlocks, TagMap
    from pyrung.click.ladder import pyrung_to_ladder

    wire = tuple(c for c in cases if c.in_process_only is None)
    skipped = tuple(c for c in cases if c.in_process_only is not None)
    if not wire:
        raise ValueError("No cases can run over Modbus.")
    if len(wire) > MAX_SLOTS:
        raise ValueError(f"At most {MAX_SLOTS} cases fit in DS; got {len(wire)}.")

    ds = ClickBlocks().ds
    slot = make_slot(len(wire))
    build_hash_tag = Int("Conformance_BuildHash")
    slot_count_tag = Int("Conformance_SlotCount")
    tag_map = TagMap(
        [
            build_hash_tag.map_to(ds[HASH_REGISTER]),
            slot_count_tag.map_to(ds[COUNT_REGISTER]),
            *slot.map_to(ds.select(SLOT_BASE, SLOT_BASE + len(wire) * SLOT_STRIDE - 1)),
        ]
    )

    with Program(strict=False) as body:
        for i, c in enumerate(wire, start=1):
            c.ladder(slot[i])
    # Validated export: a case the CLICK exporter rejects fails here, before any
    # wire traffic.  Mark such a case ``in_process_only``.
    build_hash = _hash_export(pyrung_to_ladder(body, tag_map))

    with Program(strict=False) as program:
        with Rung(system.sys.first_scan):
            copy(build_hash, build_hash_tag)
            copy(len(wire), slot_count_tag)
        for i, c in enumerate(wire, start=1):
            c.ladder(slot[i])

    return ConformanceProgram(
        cases=wire,
        skipped=skipped,
        program=program,
        tag_map=tag_map,
        slot=slot,
        build_hash=build_hash,
    )


def _hash_export(bundle: LadderBundle) -> int:
    """Fold a ladder export into a nonzero value that fits a DS register (1..32767)."""
    payload = repr((bundle.main_rows, bundle.subroutine_rows))
    digest = hashlib.sha256(payload.encode("utf-8")).digest()
    return int.from_bytes(digest[:4], "big") % 32767 + 1
