"""The checked-in layout: a fixed DS block per group and a fixed slot per case.

A manifest is TOML::

    [groups.math]
    base = 301          # group header at DS301, slot N at DS(301 + 10*N)
    slots = 19          # capacity: the block is DS301..DS500

    [groups.math.cases]
    int_wraps = 1       # case id -> slot index; never renumber
    div_by_zero = 2

New cases append with the next index; a removed case's line can stay as a
gap.  Nothing ever moves, so one group's edits never shift another group's
(or another case's) registers.
"""

from __future__ import annotations

import tomllib
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, TypeGuard

from pyrung.conformance._slot import DS_LAST, HEADER_END, block_size

if TYPE_CHECKING:
    from pyrung.conformance._case import Group

#: Slot capacity suggested for a new group (a block of 200 registers).
DEFAULT_SLOTS = 19
#: New groups are suggested at the next free ``base`` aligned to this (DS101, DS301, ...).
_BASE_ALIGN = 100


class ManifestError(ValueError):
    """The manifest is malformed, or a group or case has no entry in it."""


@dataclass(frozen=True)
class GroupLayout:
    """One group's fixed DS block."""

    name: str
    base: int
    slots: int
    #: Case id -> slot index (1-based).
    cases: Mapping[str, int] = field(default_factory=dict)

    @property
    def last_register(self) -> int:
        return self.base + block_size(self.slots) - 1


@dataclass(frozen=True)
class Manifest:
    groups: Mapping[str, GroupLayout]
    #: Where the manifest came from, for error messages.
    source: str = "manifest"

    @classmethod
    def from_dict(cls, data: Mapping[str, Any], source: str = "manifest") -> Manifest:
        """Validate parsed manifest data (the TOML document as a dict)."""
        raw_groups = data.get("groups", {})
        if not isinstance(raw_groups, Mapping):
            raise ManifestError(f"{source}: [groups] must be a table.")
        groups: dict[str, GroupLayout] = {}
        for name, raw in raw_groups.items():
            where = f"{source}: [groups.{name}]"
            if not isinstance(raw, Mapping):
                raise ManifestError(f"{where} must be a table.")
            base = raw.get("base")
            slots = raw.get("slots")
            if not _is_int(base) or base <= HEADER_END:
                raise ManifestError(
                    f"{where} base must be an integer > {HEADER_END}, got {base!r}."
                )
            if not _is_int(slots) or slots < 1:
                raise ManifestError(f"{where} slots must be an integer >= 1, got {slots!r}.")
            raw_cases = raw.get("cases", {})
            if not isinstance(raw_cases, Mapping):
                raise ManifestError(f"{where}.cases must be a table.")
            by_index: dict[int, str] = {}
            for case_id, index in raw_cases.items():
                if not _is_int(index) or not 1 <= index <= slots:
                    raise ManifestError(
                        f"{where}.cases {case_id} = {index!r}: slot must be 1..{slots} "
                        "(raise slots, or split the group)."
                    )
                if index in by_index:
                    raise ManifestError(
                        f"{where}.cases: {by_index[index]} and {case_id} share slot {index}."
                    )
                by_index[index] = case_id
            layout = GroupLayout(name, base, slots, dict(raw_cases))
            if layout.last_register > DS_LAST:
                raise ManifestError(
                    f"{where} ends at DS{layout.last_register}, past DS{DS_LAST}; lower slots."
                )
            groups[name] = layout
        ordered = sorted(groups.values(), key=lambda g: g.base)
        for before, after in zip(ordered, ordered[1:], strict=False):
            if after.base <= before.last_register:
                raise ManifestError(
                    f"{source}: [groups.{before.name}] (DS{before.base}..DS{before.last_register}) "
                    f"overlaps [groups.{after.name}] (starts DS{after.base})."
                )
        return cls(groups=groups, source=source)

    @classmethod
    def packed(cls, groups: Sequence[Group]) -> Manifest:
        """A throwaway layout: groups packed from DS11 in order, slots in case order.

        For harness tests and quick local runs only.  Adding a case moves every
        later register, so programs for a real CLICK use a checked-in manifest.
        """
        layouts: dict[str, GroupLayout] = {}
        base = HEADER_END + 1
        for g in groups:
            wire = [c.id for c in g.cases if c.in_process_only is None]
            if not wire:
                continue
            layouts[g.name] = GroupLayout(
                g.name, base, len(wire), {cid: i for i, cid in enumerate(wire, start=1)}
            )
            base += block_size(len(wire))
        return cls(groups=layouts, source="packed layout")

    def layout_for(self, group: Group) -> GroupLayout:
        """The group's block, or :class:`ManifestError` telling the developer what to add."""
        wire = [c.id for c in group.cases if c.in_process_only is None]
        layout = self.groups.get(group.name)
        if layout is None:
            base = self._next_free_base()
            slots = max(DEFAULT_SLOTS, len(wire))
            lines = [f"[groups.{group.name}]", f"base = {base}", f"slots = {slots}", ""]
            lines.append(f"[groups.{group.name}.cases]")
            lines.extend(f"{cid} = {i}" for i, cid in enumerate(wire, start=1))
            raise ManifestError(
                f"Group {group.name!r} has no block in {self.source}. Add:\n\n" + "\n".join(lines)
            )
        missing = [cid for cid in wire if cid not in layout.cases]
        if missing:
            start = max(layout.cases.values(), default=0) + 1
            lines = [f"{cid} = {i}" for i, cid in enumerate(missing, start=start)]
            last = start + len(missing) - 1
            hint = ""
            if last > layout.slots:
                hint = (
                    f"\n\nGroup {group.name!r} holds {layout.slots} slots: raise slots "
                    f"(the block may grow up to the next group's base) or split the group."
                )
            raise ManifestError(
                f"Group {group.name!r} has cases with no slot in {self.source}. "
                f"Add under [groups.{group.name}.cases]:\n\n" + "\n".join(lines) + hint
            )
        return layout

    def _next_free_base(self) -> int:
        end = max((g.last_register for g in self.groups.values()), default=HEADER_END)
        return -(-end // _BASE_ALIGN) * _BASE_ALIGN + 1


def load_manifest(path: str | Path) -> Manifest:
    """Read and validate a manifest TOML file."""
    p = Path(path)
    with p.open("rb") as handle:
        data = tomllib.load(handle)
    return Manifest.from_dict(data, source=p.name)


def _is_int(value: object) -> TypeGuard[int]:
    return isinstance(value, int) and not isinstance(value, bool)
