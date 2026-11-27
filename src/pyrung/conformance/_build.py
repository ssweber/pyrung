"""Build the canonical conformance program: one CLICK subroutine per group on fixed DS blocks.

Every scan, the main program writes the layout hash to ``DS1`` and keeps the longest
scan time in ``DS2``. It calls every group's subroutine unconditionally. Every scan,
each subroutine writes its own hash to its group header. That hash covers its exported
case rungs. Then the subroutine runs its cases. Each case starts with its commented
reset rung (see ``_ladder.py``) and then its ladder, gated by its slot's ``Cmd`` and
``Fired``.
"""

from __future__ import annotations

import dataclasses
import hashlib
import shutil
import tempfile
from collections.abc import Mapping, Sequence
from collections.abc import Set as AbstractSet
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

from pyclickplc.validation import NICKNAME_MAX_LENGTH

from pyrung.conformance._case import Case, Group
from pyrung.conformance._formula import draw_formulas
from pyrung.conformance._ladder import check_disjoint, emit_case
from pyrung.conformance._manifest import GroupLayout, Manifest
from pyrung.conformance._slot import (
    LONGEST_SCAN_REGISTER,
    PROGRAM_HASH_REGISTER,
    SLOT_FIELDS,
    SLOT_STRIDE,
    make_slot,
    slot_register,
)
from pyrung.conformance._tee import draw_tees
from pyrung.core import Int, Program, call, copy, rung, subroutine, system

if TYPE_CHECKING:
    from pyrung.click import TagMap
    from pyrung.core.structure import _NamedArrayRuntime
    from pyrung.core.tag import Tag

#: Bump when the header protocol changes, so an old program bails as a mismatch.
#: 3: slots gained ``Ready``, raised by each case's reset rung.
#: 4: main keeps the longest scan in ``DS2`` (``LONGEST_SCAN_REGISTER``).
LAYOUT_VERSION = 4

#: The findings that the export lets through. The suite pastes RAND with DF and equal
#: bounds, and hex Math constants past 16 bits, and measures what CLICK does with them.
#: ``past_write`` and ``past_clean`` run a pointer past DD1000 on purpose, so the export
#: lets through its warning for that too.
PASTE_PROBE_FINDINGS = (
    "random-bound-type",
    "random-equal-bounds",
    "math-constant-out-of-range",
    "CLK_PTR_PAST_BANK",
)


@dataclass(frozen=True)
class GroupBuild:
    """One group as built, with its subroutine, its DS block and its hash."""

    group: Group
    layout: GroupLayout
    #: Cases that run over the wire, with their slot index, in group order.
    cases: tuple[tuple[int, Case], ...]
    slot: _NamedArrayRuntime
    hash_tag: Tag
    #: What the subroutine writes to its group header every scan (1..32767).
    group_hash: int
    #: Case id -> hash of the case's own exported rows (comment, reset, ladder).
    #: A hardware confirmation holds while it matches (``confirmed.toml``).
    case_hashes: Mapping[str, str] = field(default_factory=dict)

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
        """Return the first register of the lowest used slot and the count up to the highest."""
        indexes = [i for i, _ in self.cases]
        first = self.register(min(indexes))
        return first, (max(indexes) - min(indexes) + 1) * SLOT_STRIDE


@dataclass(frozen=True)
class ConformanceProgram:
    """The program a Modbus run talks to, and the layout it was built with.

    ``groups`` are the groups with at least one case on the wire, in the order given.
    ``skipped`` holds every in-process-only case as ``(group name, case)``. The program
    leaves those cases out. ``names`` is every group name given, wire or not.
    """

    groups: tuple[GroupBuild, ...]
    skipped: tuple[tuple[str, Case], ...]
    names: tuple[str, ...]
    program: Program
    tag_map: TagMap
    #: What the main program writes to ``DS1`` every scan (1..32767).
    program_hash: int
    #: The part this program is, for messages (``None``: no part).
    part: str | None = None
    #: The longest scan, in ms, that the cases assume. On a real PLC, a run whose longest
    #: scan is past it is void (``None``: no limit).
    scan_limit_ms: int | None = None

    def group(self, name: str) -> GroupBuild:
        for g in self.groups:
            if g.name == name:
                return g
        raise KeyError(name)

    def export(self, directory: str | Path, *, drawn: bool = True) -> Path:
        """Write the whole CLICK project and return the directory.

        The project is ``main.csv``, one CSV per subroutine under ``subroutines/``, and
        ``nicknames.csv``. Cases with a ``tee`` are drawn with their pins teeing off
        the box's input wire, and cases with ``formulas`` get their Math formulas typed
        as given. With ``drawn=False``, the exporter's own rows are written for them
        instead.

        Load the project once. Afterwards, replace one group's subroutine at a time.
        """
        from pyrung.click.ladder import pyrung_to_ladder

        out = Path(directory)
        out.mkdir(parents=True, exist_ok=True)
        # A subroutine left by an earlier export into *out* would be pasted too.
        for stale in (out / "subroutines").glob("*.csv"):
            stale.unlink()
        bundle = pyrung_to_ladder(self.program, self.tag_map, allow=PASTE_PROBE_FINDINGS)
        if drawn:
            bundle = _draw_formulas(_draw_tees(bundle, self.groups), self.groups)
        bundle.write(out)
        self.tag_map.to_nickname_file(out / "nicknames.csv")
        return out

    def export_workspace(self, workspace: str | Path) -> Path:
        """Replace ``src/plc/`` in a ClickNick workspace with this program as pyrung source.

        This method generates the source the same way that ClickNick does after a CLICK
        save. The exported ladder CSVs go through :func:`~pyrung.click.ladder_to_pyrung_project`
        (``index=True``, with this program's nicknames). Rungs that did not change export
        to the same CSV, so ClickNick's Preview Changes shows only the rungs that did.
        Nothing else in the workspace changes. ``csv/`` stays CLICK's accepted snapshot,
        which is the baseline that Preview Changes diffs against.

        The function swaps in ``src/plc/`` whole, so a reader never sees half of it. It
        returns the new ``src/plc`` directory.
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
    groups: Group | Sequence[Group],
    manifest: Manifest | None = None,
    *,
    part: str | None = None,
    scan_limit_ms: int | None = None,
    main: bool = False,
    only: Mapping[str, AbstractSet[str]] | None = None,
) -> ConformanceProgram:
    """Build the canonical program for *groups* on *manifest*'s fixed DS blocks.

    Without a manifest, the groups are packed in order (:meth:`Manifest.packed`). That
    is fine for a local sim, but it moves registers whenever you add a case. *part* names
    the program in messages. On a real PLC, a run whose longest scan is past
    *scan_limit_ms* is void (``ScanTimeError``).

    With *main* every group's case rungs run in the main program, as an ``in_main``
    group's do.  *only* then maps a group name to the ids of the cases to run (a group it
    leaves out runs every case).  Each case keeps its manifest slot and the hash of its own
    rows in the whole group, so a pass confirms what a part run would.  The layout hash
    names every case, so a program of other cases is a mismatch.  A group's header holds a
    hash of its cases' hashes, which tracks just the rows the program holds.

    Raises:
        ManifestError: A wire case or group has no manifest entry. The message
            says what to add.
        ValueError: One of these:

            - no case can run over Modbus
            - group names repeat
            - two cases share memory
            - a nickname would be longer than CLICK allows
            - *main* is given a group called more than once a scan
            - *only* is given without *main*
    """
    from pyrung.click import ClickBlocks, TagMap
    from pyrung.click.ladder import pyrung_to_ladder

    group_list = [groups] if isinstance(groups, Group) else list(groups)
    picks = dict(only or {})
    if picks and not main:
        raise ValueError("Pass main=True with only=. It picks cases for a program run in main.")
    # Groups whose rungs a part runs in a subroutine and this build runs in main.
    moved: set[str] = set()
    if main:
        moved = {g.name for g in group_list if not g.in_main}
        group_list = [_in_main(g) for g in group_list]
    names = tuple(g.name for g in group_list)
    if len(set(names)) != len(names):
        raise ValueError(f"Group names repeat: {names}")
    if manifest is None:
        manifest = Manifest.packed(group_list)

    def runs(g: Group, c: Case) -> bool:
        return c.in_process_only is None and (g.name not in picks or c.id in picks[g.name])

    skipped = tuple(
        (g.name, c) for g in group_list if g.name not in picks for c in g.cases if c.in_process_only
    )
    wire_groups = [g for g in group_list if any(runs(g, c) for c in g.cases)]
    if not wire_groups:
        raise ValueError("No cases can run over Modbus.")
    check_disjoint([(g.name, c) for g in wire_groups for c in g.cases if runs(g, c)])

    ds = ClickBlocks().ds
    program_hash_tag = Int("Conf_ProgramHash")
    longest_scan_tag = Int("Conf_LongestScan")
    mappings = [
        program_hash_tag.map_to(ds[PROGRAM_HASH_REGISTER]),
        longest_scan_tag.map_to(ds[LONGEST_SCAN_REGISTER]),
    ]
    staged: list[
        tuple[
            Group,
            GroupLayout,
            tuple[tuple[int, Case], ...],
            tuple[tuple[int, Case], ...],
            _NamedArrayRuntime,
            Tag,
        ]
    ] = []
    for g in wire_groups:
        layout = manifest.layout_for(g)
        every = tuple((layout.cases[c.id], c) for c in g.cases if c.in_process_only is None)
        cases = tuple((i, c) for i, c in every if runs(g, c))
        slot = make_slot(layout.slots, name=g.title)
        hash_tag = Int(f"{g.title}_Hash")
        _check_nicknames(g, hash_tag, slot, layout.slots)
        first_slot = slot_register(layout.base, 1)
        mappings.append(hash_tag.map_to(ds[layout.base]))
        mappings.extend(slot.map_to(ds.select(first_slot, layout.last_register)))
        # Each slot's spare register is part of the group's reserved block. The harness
        # reads and zeroes slots in runs, so a target must serve it. A tag named after
        # its own address exports with no nickname, so the slot's named array still owns
        # every nickname in its block.
        for i in range(1, layout.slots + 1):
            spare = slot_register(layout.base, i) + len(SLOT_FIELDS)
            mappings.append(Int(f"DS{spare}").map_to(ds[spare]))
        staged.append((g, layout, every, cases, slot, hash_tag))
    tag_map = TagMap(mappings)

    built: list[GroupBuild] = []
    for g, layout, every, cases, slot, hash_tag in staged:
        # Hash the whole group, picked or not.  A case's hash is its rows as its part exports
        # them, and there the last case of an in-main group holds main's closing rung.
        with Program(strict=False) as body:
            if g.in_main:
                _case_rungs(g.name, every, slot)
            else:
                with subroutine(g.subroutine):
                    _case_rungs(g.name, every, slot)
        # Validated export: a case that the CLICK exporter rejects fails here, before
        # any wire traffic. Mark such a case ``in_process_only``.
        drawn = [_Drawn(g, every)]
        bundle = _draw_formulas(
            _draw_tees(pyrung_to_ladder(body, tag_map, allow=PASTE_PROBE_FINDINGS), drawn),
            drawn,
        )
        if g.in_main:
            # The rungs are main's, and the helper subroutines they call.
            rows = (("main", bundle.main_rows), *bundle.subroutine_rows)
            group_hash = _fold(repr(rows))
            case_hashes = _case_hashes(
                g.name, rows, _helper_owners(every, slot), moved=g.name in moved
            )
            if main:
                # The rows of the cases this program holds, whichever they are.
                case_hashes = {c.id: case_hashes[c.id] for _, c in cases}
                group_hash = _fold(repr(sorted(case_hashes.items())))
        else:
            group_hash = _fold(repr(bundle.subroutine_rows))
            case_hashes = _case_hashes(g.name, bundle.subroutine_rows)
        built.append(GroupBuild(g, layout, cases, slot, hash_tag, group_hash, case_hashes))

    if main:
        # The cases, not only the groups: a program of other cases of the same groups is
        # another layout.
        entries = [(*_layout_entry(g), tuple(c.id for _, c in g.cases)) for g in built]
        program_hash = _fold(repr((LAYOUT_VERSION, "main", entries)))
    else:
        program_hash = _fold(
            repr(
                (
                    LAYOUT_VERSION,
                    [_layout_entry(g) for g in built],
                )
            )
        )

    with Program(strict=False) as program:
        with rung():
            copy(program_hash, program_hash_tag)
        # The harness writes 0 here before a run and reads it after.
        scan_ms = system.sys.scan_time_current_ms
        with rung(scan_ms > longest_scan_tag):
            copy(scan_ms, longest_scan_tag)
        for gb in built:
            if gb.group.in_main:
                continue
            for _ in range(gb.group.calls):
                with rung():
                    call(gb.subroutine)
        for gb in built:
            if gb.group.in_main:
                continue
            with subroutine(gb.subroutine):
                with rung():
                    copy(gb.group_hash, gb.hash_tag)
                _case_rungs(gb.name, gb.cases, gb.slot)
        # In-main groups last: their rungs follow every call rung of main.
        for gb in built:
            if gb.group.in_main:
                with rung():
                    copy(gb.group_hash, gb.hash_tag)
                _case_rungs(gb.name, gb.cases, gb.slot)

    return ConformanceProgram(
        groups=tuple(built),
        skipped=skipped,
        names=names,
        program=program,
        tag_map=tag_map,
        program_hash=program_hash,
        part=part,
        scan_limit_ms=scan_limit_ms,
    )


def _in_main(group: Group) -> Group:
    """*group* with its case rungs in main, or ``ValueError`` when it cannot run there."""
    if group.calls != 1:
        raise ValueError(
            f"Group {group.name!r} is called {group.calls} times a scan, and its cases count "
            "calls, so they cannot run in main."
        )
    return Group(group.name, group.cases, in_main=True)


@dataclass(frozen=True)
class _Drawn:
    group: Group
    cases: tuple[tuple[int, Case], ...]


def _draw_tees(bundle, groups):
    """Return *bundle* with every ``tee`` case's pins redrawn as tees off the box's input wire."""
    main_rows = bundle.main_rows
    subroutines = list(bundle.subroutine_rows)
    for g in groups:
        group = g.group
        for _index, c in g.cases:
            if c.tee is None:
                continue
            marker = f"{group.name}/{c.id}"
            if group.in_main:
                main_rows = draw_tees(main_rows, marker, c.tee)
                continue
            at = next(
                (i for i, (name, _) in enumerate(subroutines) if name == group.subroutine), None
            )
            if at is None:
                raise ValueError(f"Case {marker} has tee= but no subroutine {group.subroutine}")
            subroutines[at] = (group.subroutine, draw_tees(subroutines[at][1], marker, c.tee))
    return dataclasses.replace(bundle, main_rows=main_rows, subroutine_rows=tuple(subroutines))


def _draw_formulas(bundle, groups):
    """Return *bundle* with each ``formulas`` case's Math formulas typed as the case gives them."""
    main_rows = bundle.main_rows
    subroutines = list(bundle.subroutine_rows)
    for g in groups:
        group = g.group
        for _index, c in g.cases:
            if not c.formulas:
                continue
            marker = f"{group.name}/{c.id}"
            if group.in_main:
                main_rows = draw_formulas(main_rows, marker, c.formulas)
                continue
            at = next(
                (i for i, (name, _) in enumerate(subroutines) if name == group.subroutine), None
            )
            if at is None:
                raise ValueError(
                    f"Case {marker} has formulas= but no subroutine {group.subroutine}"
                )
            subroutines[at] = (
                group.subroutine,
                draw_formulas(subroutines[at][1], marker, c.formulas),
            )
    return dataclasses.replace(bundle, main_rows=main_rows, subroutine_rows=tuple(subroutines))


def _layout_entry(g: GroupBuild) -> tuple[str | int, ...]:
    """Return what the group adds to the layout hash.

    A plain group adds its name and base.
    """
    if g.group.in_main:
        return (g.subroutine, g.base, "main")
    if g.group.calls == 1:
        return (g.subroutine, g.base)
    return (g.subroutine, g.base, g.group.calls)


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


def _helper_owners(cases: Sequence[tuple[int, Case]], slot: _NamedArrayRuntime) -> dict[str, str]:
    """Map each helper subroutine name to the id of the case of an in-main group that defines it."""
    owners: dict[str, str] = {}
    for index, c in cases:
        with Program(strict=False) as alone:
            _case_rungs("", [(index, c)], slot)
        for name in alone.subroutines:
            owners[name] = c.id
    return owners


def _case_hashes(
    group: str,
    subroutine_rows: Sequence[tuple[str, Sequence[Sequence[str]]]],
    owners: Mapping[str, str] | None = None,
    *,
    moved: bool = False,
) -> dict[str, str]:
    """Map each case id to the SHA-256 (12 hex digits) of its rows.

    A case's rows run from its ``Case`` comment to the next one.

    *owners* (an in-main group) names the case that defines each helper subroutine. The
    rows of that subroutine belong to that case, not to the case before them.

    *moved* says that a part runs these main rows in a subroutine. Main's closing
    ``end()`` rung is then left out, as the subroutine's ``return()`` rung is, so each
    case has the hash that it has in its part. An ``in_main`` group keeps the ``end()``
    rung, because the hashes of its part include it.

    The last case's span stops before the closing tail of the subroutine. The exporter
    ends every subroutine with one unconditional ``return()`` rung
    (``_ensure_subroutine_return_tail``). That row belongs to the group, not to the case
    before it. Without this rule, appending a case would re-hash the previous last case.
    """
    marker = f"Case {group}/"
    spans: dict[str, list[Sequence[str]]] = {}
    for name, rows in subroutine_rows:
        body = list(rows)
        if body and (_is_return_tail(body[-1]) or (moved and _is_tail(body[-1], "end()"))):
            body.pop()
        current: list[Sequence[str]] | None = None
        if owners is not None and name in owners:
            current = spans.setdefault(owners[name], [])
        for row in body:
            text = row[1] if row and row[0] == "#" and len(row) > 1 else ""
            if text.startswith(marker):
                current = spans.setdefault(text[len(marker) :], [])
            if current is not None:
                current.append(tuple(row))
    return {
        case_id: hashlib.sha256(repr(rows).encode("utf-8")).hexdigest()[:12]
        for case_id, rows in spans.items()
    }


def _is_return_tail(row: Sequence[str]) -> bool:
    """Return True for the unconditional ``return()`` rung that ends an exported subroutine."""
    return _is_tail(row, "return()")


def _is_tail(row: Sequence[str], instruction: str) -> bool:
    """Return True for an unconditional rung that holds only *instruction*."""
    return (
        len(row) > 2
        and row[0].startswith("R")
        and row[-1] == instruction
        and all(cell == "-" for cell in row[1:-1])
    )


def _fold(payload: str) -> int:
    """Return *payload* folded into a nonzero value that fits a DS register (1..32767)."""
    digest = hashlib.sha256(payload.encode("utf-8")).digest()
    return int.from_bytes(digest[:4], "big") % 32767 + 1
