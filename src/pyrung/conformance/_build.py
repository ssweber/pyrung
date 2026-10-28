"""The canonical conformance program: one CLICK subroutine per group on fixed DS blocks.

The main program writes the layout hash to ``DS1`` every scan and calls every
group's subroutine unconditionally.  Each subroutine writes its own hash (of
its exported case rungs) to its group header every scan, then runs its cases.
Each case starts with its commented reset rung (see ``_ladder.py``), then its
ladder, gated by its slot's ``Cmd``/``Fired``.
"""

from __future__ import annotations

import hashlib
import shutil
import tempfile
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from pyclickplc.validation import NICKNAME_MAX_LENGTH

from pyrung.conformance._case import Case, Group
from pyrung.conformance._ladder import check_disjoint, emit_case
from pyrung.conformance._manifest import GroupLayout, Manifest
from pyrung.conformance._slot import (
    PROGRAM_HASH_REGISTER,
    SLOT_FIELDS,
    SLOT_STRIDE,
    make_slot,
    slot_register,
)
from pyrung.core import Int, Program, Rung, call, copy, subroutine

if TYPE_CHECKING:
    from pyrung.click import TagMap
    from pyrung.core.structure import _NamedArrayRuntime
    from pyrung.core.tag import Tag

#: Bump when the header protocol changes, so an old program bails as a mismatch.
#: 3: slots gained ``Ready``, raised by each case's reset rung.
LAYOUT_VERSION = 3


@dataclass(frozen=True)
class GroupBuild:
    """One group as built: its subroutine, its DS block, and its hash."""

    group: Group
    layout: GroupLayout
    #: Cases that run over the wire, with their slot index, in group order.
    cases: tuple[tuple[int, Case], ...]
    slot: _NamedArrayRuntime
    hash_tag: Tag
    #: What the subroutine writes to its group header every scan (1..32767).
    group_hash: int

    @property
    def name(self) -> str:
        return self.group.name

    @property
    def subroutine(self) -> str:
        return self.group.subroutine

    @property
    def base(self) -> int:
        return self.layout.base

    @property
    def hash_register(self) -> int:
        return self.layout.base

    def register(self, index: int, field: str = "Cmd") -> int:
        return slot_register(self.layout.base, index, field)

    @property
    def span(self) -> tuple[int, int]:
        """First register of the lowest used slot and register count up to the highest."""
        indexes = [i for i, _ in self.cases]
        first = self.register(min(indexes))
        return first, (max(indexes) - min(indexes) + 1) * SLOT_STRIDE


@dataclass(frozen=True)
class ConformanceProgram:
    """The program a Modbus run talks to, and the layout it was built with.

    ``groups`` are the groups with at least one case on the wire, in the order
    given.  ``skipped`` holds every in-process-only case as
    ``(group name, case)``; they are left out of the program.  ``names`` is
    every group name given, wire or not.
    """

    groups: tuple[GroupBuild, ...]
    skipped: tuple[tuple[str, Case], ...]
    names: tuple[str, ...]
    program: Program
    tag_map: TagMap
    #: What the main program writes to ``DS1`` every scan (1..32767).
    program_hash: int

    def group(self, name: str) -> GroupBuild:
        for g in self.groups:
            if g.name == name:
                return g
        raise KeyError(name)

    def export(self, directory: str | Path) -> Path:
        """Write the whole CLICK project: ``main.csv``, one CSV per group subroutine
        under ``subroutines/``, and ``nicknames.csv``.

        Returns the directory.  Load it once; afterwards replace one group's
        subroutine at a time.
        """
        from pyrung.click.ladder import pyrung_to_ladder

        out = Path(directory)
        out.mkdir(parents=True, exist_ok=True)
        pyrung_to_ladder(self.program, self.tag_map).write(out)
        self.tag_map.to_nickname_file(out / "nicknames.csv")
        return out

    def export_workspace(self, workspace: str | Path) -> Path:
        """Replace ``src/plc/`` in a ClickNick workspace with this program as pyrung source.

        The source is generated the way ClickNick generates it after a CLICK
        save: the exported ladder CSVs go through
        :func:`~pyrung.click.ladder_to_pyrung_project` (``index=True``, with
        this program's nicknames), so rungs that did not change export to the
        same CSV and ClickNick's Preview Changes shows only the ones that did.
        Nothing else in the workspace is touched: ``csv/`` stays CLICK's
        accepted snapshot, the baseline Preview Changes diffs against.

        ``src/plc/`` is swapped in whole, so a reader never sees half of it.
        Returns the new ``src/plc`` directory.
        """
        from pyrung.click import ladder_to_pyrung_project

        root = Path(workspace)
        if not root.is_dir():
            raise FileNotFoundError(f"Workspace not found: {root}")
        destination = root / "src" / "plc"
        destination.parent.mkdir(parents=True, exist_ok=True)
        previous = destination.with_name(".plc.conformance-previous")
        with tempfile.TemporaryDirectory(prefix=".conformance-export-", dir=root) as stage:
            csv_dir = self.export(Path(stage) / "csv")
            project = Path(stage) / "project"
            ladder_to_pyrung_project(
                csv_dir,
                nickname_csv=csv_dir / "nicknames.csv",
                output_dir=project,
                index=True,
                workspace_kind="persistent",
            )
            if previous.exists():
                shutil.rmtree(previous)
            if destination.exists():
                destination.replace(previous)
            try:
                (project / "src" / "plc").replace(destination)
            except Exception:
                if previous.exists() and not destination.exists():
                    previous.replace(destination)
                raise
        if previous.exists():
            shutil.rmtree(previous)
        return destination


def build_program(
    groups: Group | Sequence[Group], manifest: Manifest | None = None
) -> ConformanceProgram:
    """Build the canonical program for *groups* on *manifest*'s fixed DS blocks.

    Without a manifest the groups are packed in order (:meth:`Manifest.packed`),
    which is fine for a local sim but moves registers whenever a case is added.

    Raises:
        ManifestError: A wire case or group has no manifest entry; the message
            says what to add.
        ValueError: No case can run over Modbus, group names repeat, two cases
            share memory, or a nickname would be longer than CLICK allows.
    """
    from pyrung.click import ClickBlocks, TagMap
    from pyrung.click.ladder import pyrung_to_ladder

    group_list = [groups] if isinstance(groups, Group) else list(groups)
    names = tuple(g.name for g in group_list)
    if len(set(names)) != len(names):
        raise ValueError(f"Group names repeat: {names}")
    if manifest is None:
        manifest = Manifest.packed(group_list)

    skipped = tuple((g.name, c) for g in group_list for c in g.cases if c.in_process_only)
    wire_groups = [g for g in group_list if any(c.in_process_only is None for c in g.cases)]
    if not wire_groups:
        raise ValueError("No cases can run over Modbus.")
    check_disjoint([(g.name, c) for g in wire_groups for c in g.cases if c.in_process_only is None])

    ds = ClickBlocks().ds
    program_hash_tag = Int("Conf_ProgramHash")
    mappings = [program_hash_tag.map_to(ds[PROGRAM_HASH_REGISTER])]
    staged: list[
        tuple[Group, GroupLayout, tuple[tuple[int, Case], ...], _NamedArrayRuntime, Tag]
    ] = []
    for g in wire_groups:
        layout = manifest.layout_for(g)
        cases = tuple((layout.cases[c.id], c) for c in g.cases if c.in_process_only is None)
        slot = make_slot(layout.slots, name=g.title)
        hash_tag = Int(f"{g.title}_Hash")
        _check_nicknames(g, hash_tag, slot, layout.slots)
        first_slot = slot_register(layout.base, 1)
        mappings.append(hash_tag.map_to(ds[layout.base]))
        mappings.extend(slot.map_to(ds.select(first_slot, layout.last_register)))
        # Each slot's spare register is part of the group's reserved block: the
        # harness reads and zeroes slots in runs, so a target must serve it.  A
        # tag named after its own address exports with no nickname, so the
        # slot's named array still owns every nickname in its block.
        for i in range(1, layout.slots + 1):
            spare = slot_register(layout.base, i) + len(SLOT_FIELDS)
            mappings.append(Int(f"DS{spare}").map_to(ds[spare]))
        staged.append((g, layout, cases, slot, hash_tag))
    tag_map = TagMap(mappings)

    built: list[GroupBuild] = []
    for g, layout, cases, slot, hash_tag in staged:
        with Program(strict=False) as body:
            with subroutine(g.subroutine):
                _case_rungs(g.name, cases, slot)
        # Validated export: a case the CLICK exporter rejects fails here, before
        # any wire traffic.  Mark such a case ``in_process_only``.
        bundle = pyrung_to_ladder(body, tag_map)
        group_hash = _fold(repr(bundle.subroutine_rows))
        built.append(GroupBuild(g, layout, cases, slot, hash_tag, group_hash))

    program_hash = _fold(repr((LAYOUT_VERSION, [(g.subroutine, g.base) for g in built])))

    with Program(strict=False) as program:
        with Rung():
            copy(program_hash, program_hash_tag)
        for gb in built:
            with Rung():
                call(gb.subroutine)
        for gb in built:
            with subroutine(gb.subroutine):
                with Rung():
                    copy(gb.group_hash, gb.hash_tag)
                _case_rungs(gb.name, gb.cases, gb.slot)

    return ConformanceProgram(
        groups=tuple(built),
        skipped=skipped,
        names=names,
        program=program,
        tag_map=tag_map,
        program_hash=program_hash,
    )


def _case_rungs(group: str, cases: Sequence[tuple[int, Case]], slot: _NamedArrayRuntime) -> None:
    for index, c in cases:
        emit_case(group, c, slot, index)


def _check_nicknames(group: Group, hash_tag: Tag, slot: _NamedArrayRuntime, slots: int) -> None:
    longest_field = max(SLOT_FIELDS, key=len)
    for name in (hash_tag.name, getattr(slot[slots], longest_field).name):
        if len(name) > NICKNAME_MAX_LENGTH:
            raise ValueError(
                f"Group {group.name!r} makes nickname {name!r} ({len(name)} chars); CLICK allows "
                f"{NICKNAME_MAX_LENGTH}. Use a shorter group name."
            )


def _fold(payload: str) -> int:
    """Fold *payload* into a nonzero value that fits a DS register (1..32767)."""
    digest = hashlib.sha256(payload.encode("utf-8")).digest()
    return int.from_bytes(digest[:4], "big") % 32767 + 1
