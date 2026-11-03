"""Patch/force override state for PLC."""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping
from contextlib import contextmanager
from types import MappingProxyType
from typing import TYPE_CHECKING, NamedTuple

from pyrung.core.instruction.conversions import copy_store_transform
from pyrung.core.storage import check_real_value, store_char, store_real, store_value
from pyrung.core.tag import Tag as TagClass
from pyrung.core.tag import TagType

if TYPE_CHECKING:
    from collections.abc import Iterator
    from typing import Any

    from pyrung.core.context import ScanContext
    from pyrung.core.expression import RandIntExpr
    from pyrung.core.tag import Tag


def _value_a_type_holds(tag_type: TagType, value: Any, what: str) -> Any:
    """What a tag of *tag_type* holds when a patch or force writes *value*.

    The value is stored as a ``copy()`` stores it: a tag holds its own type, so it can
    always serve it (a Modbus read packs by the tag's type, and a float into an INT
    raises there).  A value no store of that type can make sense of is refused rather
    than guessed at, as is a finite REAL no float32 holds.
    """
    if tag_type is TagType.CHAR:
        held = store_char(value)
        if not isinstance(held, str) or len(held) != 1:
            raise ValueError(f"{what}: a CHAR holds one character, not {value!r}")
        return held
    if not isinstance(value, bool | int | float):
        raise ValueError(f"{what}: {tag_type.name} holds a number, not {value!r}")
    if tag_type is TagType.REAL:
        # +/-Inf and NaN are accepted: a REAL holds them (a Math overflow stores
        # +/-Inf), so a patch or force can put one back.
        check_real_value(value, what, allow_non_finite=True)
    transform = copy_store_transform(tag_type)
    return value if transform is None else store_value(value, transform)


def refuses_value(tag: Tag, value: Any) -> bool:
    """Whether a patch of *tag* with *value* raises."""
    try:
        _value_a_type_holds(tag.type, value, "candidate")
    except ValueError:
        return True
    return False


def held_candidates(tag: Tag, values: Iterable[Any]) -> set[Any]:
    """What *tag* holds of the *values* an analysis would try on it.

    A candidate comes off a rung or a ``choices=`` as written, and the tag may not
    hold that.  A number is mapped to what its store holds rather than dropped, so
    the values around a literal keep both sides of a compare: ``I > 3.5`` splits into
    3 and 4.  A finite REAL past float32 range maps to +/-Inf, which a REAL holds and
    which is past that literal too, though a patch refuses the literal itself.  What
    no store of the type can make sense of (``"AB"`` on a CHAR, ``"x"`` on an INT) is
    dropped: no scan can see the tag hold it.  A number a REAL holds as an equal float
    stays as it was given: a ``-1`` on a REAL is not rewritten to ``-1.0`` in what a plan
    or a witness reports.  Anywhere else the candidate is the stored value, so ``3.0`` on
    an INT is 3.
    """
    held: set[Any] = set()
    for value in values:
        if tag.type is TagType.REAL and isinstance(value, bool | int | float):
            stored = store_real(value)
            held.add(value if value == stored and not isinstance(value, bool) else stored)
        elif not refuses_value(tag, value):
            held.add(_value_a_type_holds(tag.type, value, "candidate"))
    return held


class _RungsWalked(NamedTuple):
    named: tuple[Tag, ...]
    edge_names: frozenset[str]
    block_uses: list[Any]
    # What the walk found going on from the rungs into a block's cache or a program's
    # own attributes: what has been asked of them so far, not what the rungs say.
    beyond_rungs: tuple[Tag, ...]


def _addresses_reached(use: Any) -> range | tuple[int, ...]:
    from pyrung.core.memory_block import BlockRange

    if isinstance(use, BlockRange):
        return use.addresses
    return use.block._window_addresses(use.block.start, use.block.end)


def _slot_is_retentive(block: Any, addr: int) -> bool:
    tag = block._tag_cache.get(addr)
    if tag is None:
        tag = block._mapped_tags.get(addr)
    if tag is not None:
        return tag.retentive
    return block._slot_field(addr, "retentive", block.retentive)


def _refusal(block: Any, addr: int) -> Exception | None:
    if block._slot_asks_factory(addr):
        try:
            block._factory_default(addr)
        except Exception as refusal:
            return refusal
    return None


def _addresses_that_declare(block: Any) -> list[int]:
    of_the_field = (
        block._pyrung_field_min is not None
        or block._pyrung_field_max is not None
        or block._pyrung_field_choices is not None
        or block._pyrung_field_physical is not None
    )
    found = []
    for addr in (
        block._window_addresses(block.start, block.end) if of_the_field else block._slot_config
    ):
        hints = block._effective_slot_hints(addr)
        if (
            hints.min is not None
            or hints.max is not None
            or hints.choices is not None
            or hints.physical is not None
        ):
            found.append(addr)
    return found


class TagsRungsReach:
    """The tags a program's rungs name, and the block slots a range or pointer reaches.

    What a tag patched by name holds comes from here, on either engine, and so does what a
    reset keeps and whose limits and plants count.  It is read off the rungs alone: a
    ``select()`` or a ``Block[Pointer]`` names only its block, whose slots stay lazy until
    something asks for one, and something having asked says nothing about this program.
    Nothing here makes a slot into a tag unless a tag is what was asked for: a slot's
    factory may count its calls or refuse an address, and a slot not yet a tag can still
    be configured.  Walked the first time it is asked, and shared with forks, which skip
    the walk.
    """

    __slots__ = (
        "_logic",
        "_program",
        "_walked",
        "_named_by_name",
        "_reached",
        "_slots",
        "_slots_as_of",
    )

    def __init__(self, logic: Iterable[Any], program: Any) -> None:
        self._logic = logic
        self._program = program
        self._walked: _RungsWalked | None = None
        self._named_by_name: dict[str, Tag] | None = None
        self._reached: list[tuple[Any, set[int]]] | None = None
        self._slots: dict[str, tuple[Any, int]] = {}
        self._slots_as_of: int = -1

    def get(self, name: str) -> Tag | None:
        slot = self._slots_by_name().get(name)
        if slot is not None:
            block, addr = slot
            return block._get_tag(addr)
        return self._named().get(name)

    def type_of(self, name: str) -> TagType | None:
        slot = self._slots_by_name().get(name)
        if slot is not None:
            block, addr = slot
            mapped = block._mapped_tags.get(addr)
            return block.type if mapped is None else mapped.type
        tag = self._named().get(name)
        return None if tag is None else tag.type

    def names(self) -> set[str]:
        return {*self._slots_by_name(), *self._named()}

    def refusal_of(self, name: str) -> Exception | None:
        """What a slot's factory raises for it, if *name* is a slot its factory refuses."""
        slot = self._slots_by_name().get(name)
        return None if slot is None else _refusal(*slot)

    def holds(self, name: str) -> bool:
        """Whether *name* is reached, and not a slot its factory refuses.

        Only the factory can say.  It is asked in address order, up to this slot, so a factory
        that counts gives the slots what reading them in order gives, and no tag is made.
        """
        slot = self._slots_by_name().get(name)
        if slot is None:
            return name in self._named()
        block, addr = slot
        if block.default_factory is None:
            return True
        refusal = None
        for earlier in sorted(self._addresses_of(block)):
            if earlier <= addr:
                refusal = _refusal(block, earlier)
        return refusal is None

    def refused(self) -> set[str]:
        """The slots reached that their factories refuse, every factory asked as ``holds`` asks."""
        self._slots_by_name()
        found: set[str] = set()
        for block, addresses in self._reached or ():
            if block.default_factory is not None:
                for addr in sorted(addresses):
                    if _refusal(block, addr) is not None:
                        found.add(block._effective_slot_name(addr))
        return found

    def made(self, name: str) -> Tag | None:
        """The tag for *name* if there already is one.  Asks nothing."""
        slot = self._slots_by_name().get(name)
        if slot is not None:
            return slot[0]._tag_cache.get(slot[1])
        return self._named().get(name)

    def _addresses_of(self, block: Any) -> set[int]:
        for reached, addresses in self._reached or ():
            if reached is block:
                return addresses
        return set()

    def declared(self) -> dict[str, Tag]:
        """The slots reached that declare limits, choices or a plant.

        Only these become tags here: the rest of a block stays lazy, and a slot can be
        configured until something asks for it.
        """
        found: dict[str, Tag] = {}
        declaring_by_block: dict[int, list[int]] = {}
        for use in self.walk().block_uses:
            block = use.block
            declaring = declaring_by_block.get(id(block))
            if declaring is None:
                declaring = declaring_by_block[id(block)] = _addresses_that_declare(block)
            if declaring:
                reached = _addresses_reached(use)
                for addr in declaring:
                    if addr in reached:
                        tag = block._get_tag(addr)
                        found[tag.name] = tag
        return found

    def declared_with(self, known: Mapping[str, Tag]) -> dict[str, Tag]:
        """The slots that declare, and of *known* the tags more than a cached slot put there."""
        beyond = {tag.name for tag in self.walk().beyond_rungs}
        return {
            **self.declared(),
            **{name: tag for name, tag in known.items() if name not in beyond},
        }

    def kept_through_a_reset(
        self,
        current: Mapping[str, Any],
        rebuilt: dict[str, Any],
        *,
        preserve_all: bool,
        preserve_retentive: bool,
    ) -> None:
        """Add to *rebuilt* the reached slots a reset keeps the value of.

        One it does not keep is left out: it holds its default, and stays lazy.
        """
        if not preserve_all and not preserve_retentive:
            return
        slots = self._slots_by_name()
        named = self._named()
        for name, value in current.items():
            if name in rebuilt:
                continue
            slot = slots.get(name)
            if slot is not None:
                retentive = _slot_is_retentive(*slot)
            elif name in named:
                retentive = named[name].retentive
            else:
                continue
            if preserve_all or retentive:
                rebuilt[name] = value

    def walk(self) -> _RungsWalked:
        if self._walked is None:
            from pyrung.core.runner import _iter_tags_and_edge_names

            roots: list[Any] = list(self._logic)
            if self._program is not None:
                for subroutine_rungs in self._program.subroutines.values():
                    roots.extend(subroutine_rungs)
            uses: list[Any] = []
            beyond: list[Tag] = []
            named, edges = _iter_tags_and_edge_names(roots, block_uses=uses, beyond_rungs=beyond)
            self._walked = _RungsWalked(named, edges, uses, tuple(beyond))
        return self._walked

    def _named(self) -> dict[str, Tag]:
        if self._named_by_name is None:
            from pyrung.core.system_points import SYSTEM_TAGS_BY_NAME

            self._named_by_name = {
                tag.name: tag for tag in self.walk().named if tag.name not in SYSTEM_TAGS_BY_NAME
            }
        return self._named_by_name

    def _slots_by_name(self) -> dict[str, tuple[Any, int]]:
        """(block, address) by name, of each slot reached.  Asks no block for a tag."""
        if self._reached is None:
            by_block: dict[int, tuple[Any, set[int]]] = {}
            for use in self.walk().block_uses:
                entry = by_block.setdefault(id(use.block), (use.block, set()))
                entry[1].update(_addresses_reached(use))
            self._reached = list(by_block.values())
        as_of = sum(block._slot_names_changed for block, _ in self._reached)
        if as_of != self._slots_as_of:
            from pyrung.core.system_points import SYSTEM_TAGS_BY_NAME

            slots: dict[str, tuple[Any, int]] = {}
            for block, addresses in self._reached:
                for addr in addresses:
                    name = block._effective_slot_name(addr)
                    if name not in SYSTEM_TAGS_BY_NAME:
                        slots[name] = (block, addr)
            self._slots = slots
            self._slots_as_of = as_of
        return self._slots


class TagsKnownOrReached(Mapping[str, "Tag"]):
    """``plc.tags``: the tags a runner knows, and what its rungs reach, read as asked.

    A slot its block's factory refuses is not in it.  Reading a slot makes it a tag, and so
    does what asks for the tags themselves (``copy()``, ``|``, ``values()``, ``items()``).
    Names alone (``in``, iterating, ``len()``, ``reversed()``) make none, and ``repr()`` asks
    no factory either.
    """

    __slots__ = ("_known", "_reach")

    def __init__(self, known: Mapping[str, Tag], reach: TagsRungsReach) -> None:
        self._known = known
        self._reach = reach

    def __getitem__(self, name: str) -> Tag:
        tag = self._known.get(name)
        if tag is None:
            refusal = self._reach.refusal_of(name)
            if refusal is not None:
                raise KeyError(name) from refusal
            tag = self._reach.get(name)
        if tag is None:
            raise KeyError(name)
        return tag

    def __contains__(self, name: object) -> bool:
        return isinstance(name, str) and (name in self._known or self._reach.holds(name))

    def __iter__(self) -> Iterator[str]:
        refused = self._reach.refused()
        yield from self._known
        yield from sorted(self._reach.names() - self._known.keys() - refused)

    def __len__(self) -> int:
        return len((self._reach.names() - self._reach.refused()) | self._known.keys())

    def __reversed__(self) -> Iterator[str]:
        return reversed(list(self))

    def __or__(self, other: Mapping[str, Any]) -> dict[str, Any]:
        if not isinstance(other, Mapping):
            return NotImplemented
        return {**self, **other}

    def __ror__(self, other: Mapping[str, Any]) -> dict[str, Any]:
        if not isinstance(other, Mapping):
            return NotImplemented
        return {**other, **self}

    def copy(self) -> dict[str, Tag]:
        return dict(self)

    def __repr__(self) -> str:
        shown = [f"{name!r}: {tag!r}" for name, tag in self._known.items()]
        for name in sorted(self._reach.names() - self._known.keys()):
            tag = self._reach.made(name)
            shown.append(f"{name!r}: {'<not read yet>' if tag is None else repr(tag)}")
        return "{" + ", ".join(shown) + "}"


class InputOverrideManager:
    """Encapsulates one-shot patches and persistent force overrides."""

    def __init__(
        self,
        *,
        is_read_only: Callable[[str], bool],
        known_type: Callable[[str], TagType | None] | None = None,
    ) -> None:
        self._is_read_only = is_read_only
        self._known_type = known_type
        self._pending_patches: dict[str, bool | int | float | str] = {}
        self._forces: dict[str, bool | int | float | str] = {}

    @property
    def pending_patches(self) -> dict[str, bool | int | float | str]:
        return self._pending_patches

    @property
    def forces_mutable(self) -> dict[str, bool | int | float | str]:
        return self._forces

    def _normalize_tag_name(self, tag: str | Tag, *, method: str) -> str:
        if isinstance(tag, TagClass):
            return tag.name
        if isinstance(tag, str):
            return tag
        raise TypeError(f"{method}() keys must be str or Tag, got {type(tag).__name__}")

    def _normalize_tag_updates(
        self,
        tags: Mapping[str, bool | int | float | str]
        | Mapping[Tag, bool | int | float | str]
        | Mapping[str | Tag, bool | int | float | str],
        *,
        method: str,
    ) -> dict[str, bool | int | float | str]:
        normalized: dict[str, bool | int | float | str] = {}
        for key, value in tags.items():
            name = self._normalize_tag_name(key, method=method)
            if self._is_read_only(name):
                raise ValueError(f"Tag '{name}' is read-only system point and cannot be written")
            normalized[name] = self._held(key, name, value, method=method)
        return normalized

    def _held(
        self, key: str | Tag, name: str, value: bool | int | float | str, *, method: str
    ) -> bool | int | float | str:
        tag_type = key.type if isinstance(key, TagClass) else None
        if tag_type is None and self._known_type is not None:
            tag_type = self._known_type(name)
        if tag_type is None:
            # A name no rung reads has no type here.  Only a CHAR can be written "":
            # a blank stores NUL.
            return store_char(value)
        return _value_a_type_holds(tag_type, value, f"{method}() of {name}")

    def patch(
        self,
        tags: Mapping[str, bool | int | float | str]
        | Mapping[Tag, bool | int | float | str]
        | Mapping[str | Tag, bool | int | float | str],
    ) -> None:
        self._pending_patches.update(self._normalize_tag_updates(tags, method="patch"))

    def restore_patch(self, recorded: Mapping[str, bool | int | float | str]) -> None:
        """Queue a patch from a scan log: what it recorded is what that scan was given.

        Judged when it was made, under what the runner knew by that name then.  A replay
        may know more (a kernel knows every slot of a range), so judging it again would
        turn a replayed scan into a different scan.
        """
        self._pending_patches.update(recorded)

    def add_force(self, tag: str | Tag, value: bool | int | float | str) -> None:
        name = self._normalize_tag_name(tag, method="add_force")
        if self._is_read_only(name):
            raise ValueError(f"Tag '{name}' is read-only system point and cannot be written")
        self._forces[name] = self._held(tag, name, value, method="force")

    def remove_force(self, tag: str | Tag) -> None:
        name = self._normalize_tag_name(tag, method="remove_force")
        if name not in self._forces:
            raise KeyError(name)
        del self._forces[name]

    def clear_forces(self) -> None:
        self._forces.clear()

    @contextmanager
    def force(
        self,
        overrides: Mapping[str, bool | int | float | str]
        | Mapping[Tag, bool | int | float | str]
        | Mapping[str | Tag, bool | int | float | str],
    ) -> Iterator[None]:
        snapshot = self._forces.copy()
        try:
            for tag, value in overrides.items():
                self.add_force(tag, value)
            yield None
        finally:
            self._forces.clear()
            self._forces.update(snapshot)

    @property
    def forces(self) -> Mapping[str, bool | int | float | str]:
        return MappingProxyType(self._forces)

    def get_live_override(self, name: str) -> tuple[bool, Any]:
        if name in self._pending_patches:
            return True, self._pending_patches[name]
        if name in self._forces:
            return True, self._forces[name]
        return False, None

    def apply_pre_scan(self, ctx: ScanContext) -> dict[str, bool | int | float | str]:
        """Drain patches and apply forces.  Returns a copy of the patches
        that were drained this scan (empty if none), for the recorder.
        """
        drained: dict[str, bool | int | float | str] = {}
        if self._pending_patches:
            drained = dict(self._pending_patches)
            ctx.set_tags(self._pending_patches)
            self._pending_patches.clear()

        if self._forces:
            self._apply_forces_if_changed(ctx)
        return drained

    def apply_randint_draws(
        self,
        ctx: ScanContext,
        sites: tuple[RandIntExpr, ...],
        drained: Mapping[str, object],
    ) -> tuple[str, ...]:
        """Write this scan's ``randint`` draws after the drain.

        A site whose tag was patched this scan or is forced keeps that value
        (the test override).  Draws are a pure function of the scan id, so they
        are not recorded: replay recomputes them.  Returns the drawn names.
        """
        draws = {
            site.tag.name: site.draw(ctx)
            for site in sites
            if site.tag.name not in drained and site.tag.name not in self._forces
        }
        if draws:
            ctx.set_tags(draws)
        return tuple(draws)

    def apply_post_logic(self, ctx: ScanContext) -> None:
        if self._forces:
            self._apply_forces_if_changed(ctx)

    def _apply_forces_if_changed(self, ctx: ScanContext) -> None:
        """Write only forces whose value differs from current pending/state.

        Skipping already-matching writes keeps the ``tags`` PMap structurally
        shared when a force's value is stable.
        """
        diff = {name: value for name, value in self._forces.items() if ctx.get_tag(name) != value}
        if diff:
            ctx.set_tags(diff)
