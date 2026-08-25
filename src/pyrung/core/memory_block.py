"""Block-based memory regions and indirect addressing.

Block provides factory methods for creating Tags from typed memory regions.
IndirectRef enables pointer/indirect addressing resolved at runtime.
BlockRange represents contiguous ranges for block operations.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, ClassVar, Final, Literal, NamedTuple, Never, cast, overload

from pyrung.core.physical import Physical
from pyrung.core.tag import (
    ChoiceMap,
    LiveInputTag,
    LiveOutputTag,
    LiveTag,
    Tag,
    TagType,
    _normalize_choices,
)

if TYPE_CHECKING:
    from pyrung.core.condition import (
        Condition,
        IndirectCompareGe,
        IndirectCompareGt,
        IndirectCompareLe,
        IndirectCompareLt,
    )
    from pyrung.core.context import ConditionView, ScanContext
    from pyrung.core.expression import Expression, SumExpr
    from pyrung.core.state import SystemState
    from pyrung.core.tag import MappingEntry


UNSET: Final = object()


@dataclass
class SlotConfig:
    """Per-slot override entry; fields left as ``UNSET`` inherit block defaults."""

    name: Any = UNSET
    retentive: Any = UNSET
    default: Any = UNSET
    comment: Any = UNSET
    choices: Any = UNSET
    readonly: Any = UNSET
    external: Any = UNSET
    final: Any = UNSET
    public: Any = UNSET
    lock: Any = UNSET
    physical: Any = UNSET
    link: Any = UNSET
    min: Any = UNSET
    max: Any = UNSET
    uom: Any = UNSET


class _SlotHints(NamedTuple):
    choices: ChoiceMap | None
    readonly: bool
    external: bool
    final: bool
    public: bool
    lock: bool
    physical: Physical | None = None
    link: str | None = None
    min: int | float | None = None
    max: int | float | None = None
    uom: str | None = None


class SlotView:
    """Live view into a single block slot.

    Returned by ``block.slot(addr)``.  Properties reflect the *current*
    effective policy (inherited defaults + overrides).  Call ``.reset()``
    to clear all overrides and restore inherited defaults.
    """

    __slots__ = ("_block", "_addr")

    def __init__(self, block: Block, addr: int) -> None:
        self._block = block
        self._addr = addr

    @property
    def name(self) -> str:
        return self._block._effective_slot_name(self._addr)

    @property
    def retentive(self) -> bool:
        return self._block._effective_slot_policy(self._addr)[0]

    @property
    def default(self) -> Any:
        return self._block._effective_slot_policy(self._addr)[1]

    @property
    def comment(self) -> str:
        return self._block._effective_slot_comment(self._addr)

    @property
    def choices(self) -> ChoiceMap | None:
        return self._block._effective_slot_hints(self._addr).choices

    @property
    def readonly(self) -> bool:
        return self._block._effective_slot_hints(self._addr).readonly

    @property
    def external(self) -> bool:
        return self._block._effective_slot_hints(self._addr).external

    @property
    def final(self) -> bool:
        return self._block._effective_slot_hints(self._addr).final

    @property
    def public(self) -> bool:
        return self._block._effective_slot_hints(self._addr).public

    @property
    def lock(self) -> bool:
        return self._block._effective_slot_hints(self._addr).lock

    @property
    def physical(self) -> Physical | None:
        return self._block._effective_slot_hints(self._addr).physical

    @property
    def link(self) -> str | None:
        return self._block._effective_slot_hints(self._addr).link

    @property
    def min(self) -> int | float | None:
        return self._block._effective_slot_hints(self._addr).min

    @property
    def max(self) -> int | float | None:
        return self._block._effective_slot_hints(self._addr).max

    @property
    def uom(self) -> str | None:
        return self._block._effective_slot_hints(self._addr).uom

    def _overridden(self, field_name: str) -> bool:
        entry = self._block._slot_config.get(self._addr)
        return entry is not None and getattr(entry, field_name) is not UNSET

    @property
    def name_overridden(self) -> bool:
        return self._overridden("name")

    @property
    def retentive_overridden(self) -> bool:
        return self._overridden("retentive")

    @property
    def default_overridden(self) -> bool:
        return self._overridden("default")

    @property
    def comment_overridden(self) -> bool:
        return self._overridden("comment")

    @property
    def choices_overridden(self) -> bool:
        return self._overridden("choices")

    @property
    def readonly_overridden(self) -> bool:
        return self._overridden("readonly")

    @property
    def external_overridden(self) -> bool:
        return self._overridden("external")

    @property
    def final_overridden(self) -> bool:
        return self._overridden("final")

    @property
    def public_overridden(self) -> bool:
        return self._overridden("public")

    @property
    def lock_overridden(self) -> bool:
        return self._overridden("lock")

    @property
    def physical_overridden(self) -> bool:
        return self._overridden("physical")

    @property
    def link_overridden(self) -> bool:
        return self._overridden("link")

    @property
    def min_overridden(self) -> bool:
        return self._overridden("min")

    @property
    def max_overridden(self) -> bool:
        return self._overridden("max")

    @property
    def uom_overridden(self) -> bool:
        return self._overridden("uom")

    def reset(self) -> None:
        """Clear all overrides, restoring inherited defaults."""
        self._block._assert_not_materialized(self._addr, action="reset slot")
        self._block._slot_config.pop(self._addr, None)

    def __repr__(self) -> str:
        return (
            f"SlotView({self._block.name}[{self._addr}], "
            f"name={self.name!r}, retentive={self.retentive})"
        )


class RangeSlotView:
    """Live view into a range of block slots.

    Returned by ``block.slot(start, end)``.  Call ``.reset()`` to clear
    all per-slot overrides for every address in the range.
    """

    __slots__ = ("_block", "_start", "_end")

    def __init__(self, block: Block, start: int, end: int) -> None:
        self._block = block
        self._start = start
        self._end = end

    def reset(self) -> None:
        """Clear all overrides for every address in this range."""
        addresses = self._block._window_addresses(self._start, self._end)
        for addr in addresses:
            self._block._assert_not_materialized(addr, action="reset slot")
        for addr in addresses:
            self._block._slot_config.pop(addr, None)

    def __repr__(self) -> str:
        return f"RangeSlotView({self._block.name}[{self._start}:{self._end}])"


_TYPE_DEFAULTS: dict[TagType, Any] = {
    TagType.BOOL: False,
    TagType.INT: 0,
    TagType.DINT: 0,
    TagType.REAL: 0.0,
    TagType.WORD: 0,
    TagType.CHAR: "",
}


@dataclass(eq=False)
class Block:
    """Factory for creating Tags from a typed memory region.

    `Block` defines a named, 1-indexed memory region where every address shares
    the same `TagType`. Indexing a `Block` returns a cached `LiveTag`. The block
    holds no runtime values — all values live in `SystemState.tags`.

    Address bounds are **inclusive** on both ends: `Block("DS", INT, 1, 100)`
    defines addresses 1–100 (100 tags). Indexing outside this range raises
    `IndexError`. Slice syntax (`block[1:10]`) is rejected — use
    `.select(start, end)` instead.

    For sparse blocks (e.g. Click X/Y banks with non-contiguous valid addresses),
    pass `valid_ranges` to restrict which addresses within `[start, end]` are
    legal.

    Args:
        name: Block prefix used to generate tag names (e.g. ``"DS"`` →
            ``"DS1"``, ``"DS2"`` …).
        type: `TagType` shared by all tags in this block.
        start: Inclusive lower bound address (must be ≥ 0).
        end: Inclusive upper bound address (must be ≥ start).
        retentive: Whether tags in this block survive power cycles.
            Default ``False``.
        valid_ranges: Optional tuple of ``(lo, hi)`` inclusive segments
            that constrain which addresses within ``[start, end]`` are
            accessible. Addresses outside all segments raise `IndexError`.
        address_formatter: Optional callable ``(block_name, addr) → str``
            that overrides default tag name generation. Used by dialects
            for canonical display names like ``"X001"``.

    Example:
        ```python
        DS = Block("DS", TagType.INT, 1, 100)
        DS[1]          # → LiveTag("DS1", TagType.INT)
        DS[101]        # → IndexError

        # Range for block operations:
        DS.select(1, 10)   # → BlockRange, tags DS1..DS10

        # Indirect (pointer) addressing:
        idx = Int("Idx")
        DS[idx]        # → IndirectRef, resolved at scan time
        DS[idx + 1]    # → IndirectExprRef
        ```
    """

    name: str
    type: TagType
    start: int
    end: int
    retentive: bool = False
    valid_ranges: tuple[tuple[int, int], ...] | None = None
    address_formatter: Callable[[str, int], str] | None = None
    default_factory: Callable[[int], Any] | None = None
    _tag_cache: dict[int, LiveTag] = field(default_factory=dict, repr=False)
    _slot_config: dict[int, SlotConfig] = field(default_factory=dict, repr=False)
    _mapped_tags: dict[int, Tag] = field(default_factory=dict, repr=False)
    # logical addr -> (hardware block, hardware addr), set by map_to when this
    # block is mapped onto a hardware bank range. The bank owns the register;
    # this block is a named window into it.
    _hw_alias: dict[int, tuple[Block, int]] = field(default_factory=dict, repr=False)
    _pyrung_structure_runtime: Any | None = field(default=None, init=False, repr=False)
    _pyrung_structure_kind: Literal["udt", "named_array"] | None = field(
        default=None, init=False, repr=False
    )
    _pyrung_structure_name: str | None = field(default=None, init=False, repr=False)
    _pyrung_structure_field: str | None = field(default=None, init=False, repr=False)
    _pyrung_field_choices: ChoiceMap | None = field(default=None, init=False, repr=False)
    _pyrung_field_readonly: bool = field(default=False, init=False, repr=False)
    _pyrung_field_external: bool = field(default=False, init=False, repr=False)
    _pyrung_field_final: bool = field(default=False, init=False, repr=False)
    _pyrung_field_public: bool = field(default=False, init=False, repr=False)
    _pyrung_field_lock: bool = field(default=False, init=False, repr=False)
    _pyrung_field_physical: Physical | None = field(default=None, init=False, repr=False)
    _pyrung_field_link: str | None = field(default=None, init=False, repr=False)
    _pyrung_field_min: int | float | None = field(default=None, init=False, repr=False)
    _pyrung_field_max: int | float | None = field(default=None, init=False, repr=False)
    _pyrung_field_uom: str | None = field(default=None, init=False, repr=False)
    _pyrung_click_bg_color: str | None = field(default=None, init=False, repr=False)

    _all_instances: ClassVar[set[Block]] = set()

    def __post_init__(self):
        Block._all_instances.add(self)
        if self.start < 0:
            raise ValueError(f"start must be >= 0, got {self.start}")
        if self.end < self.start:
            raise ValueError(f"end ({self.end}) must be >= start ({self.start})")
        if self.valid_ranges is None:
            return
        for lo, hi in self.valid_ranges:
            if lo > hi:
                raise ValueError(f"valid range segment must have lo <= hi, got ({lo}, {hi})")
            if lo < self.start or hi > self.end:
                raise ValueError(
                    f"valid range segment ({lo}, {hi}) must be within {self.start}-{self.end}"
                )

    @overload
    def __getitem__(self, key: int) -> LiveTag: ...

    @overload
    def __getitem__(self, key: slice) -> Never: ...

    @overload
    def __getitem__(self, key: Tag) -> IndirectRef: ...

    @overload
    def __getitem__(self, key: Expression) -> IndirectExprRef: ...

    @overload
    def __getitem__(self, key: object) -> LiveTag | IndirectRef | IndirectExprRef: ...

    def __getitem__(self, key: int | slice | Tag | Any) -> LiveTag | IndirectRef | IndirectExprRef:
        """Access tags by address, pointer tag, or expression.

        Args:
            key: Address (int), pointer tag (Tag), or expression for computed address.

        Returns:
            - int: Single Tag (cached)
            - Tag: IndirectRef for pointer addressing
            - Expression: IndirectExprRef for computed address (e.g., DS[idx + 1])
            - slice: raises TypeError

        Raises:
            IndexError: If int address is out of range.
            TypeError: If key is a slice or invalid type.
        """
        from pyrung.core.expression import Expression

        if isinstance(key, int):
            self._validate_address(key)
            return self._get_tag(key)
        elif isinstance(key, slice):
            raise TypeError("Use .select(start, end) instead of slice syntax")
        elif isinstance(key, Expression):
            return IndirectExprRef(self, key)
        elif isinstance(key, Tag):
            return IndirectRef(self, key)
        else:
            raise TypeError(
                f"Invalid key type: {type(key).__name__}. Expected int, Tag, or Expression."
            )

    def _get_tag(self, addr: int) -> LiveTag:
        """Get or create a Tag for the given address."""
        if addr not in self._tag_cache:
            mapped = self._mapped_tags.get(addr)
            if mapped is not None:
                self._tag_cache[addr] = self._annotate_tag(cast(LiveTag, mapped), addr)
            else:
                retentive, default = self._effective_slot_policy(addr)
                comment = self._effective_slot_comment(addr)
                hints = self._effective_slot_hints(addr)
                tag = self._new_tag_for_slot(
                    addr,
                    retentive=retentive,
                    default=default,
                    comment=comment,
                    choices=hints.choices,
                    readonly=hints.readonly,
                    external=hints.external,
                    final=hints.final,
                    public=hints.public,
                    lock=hints.lock,
                    physical=hints.physical,
                    link=hints.link,
                    min=hints.min,
                    max=hints.max,
                    uom=hints.uom,
                )
                # A plain block slot receives its type default as a concrete
                # value before Tag.__post_init__, so preserve whether that value
                # was actually configured.  Structure factories return None for
                # an omitted field default and a value for an explicit one.
                configured_default = self._slot_field(addr, "default", UNSET)
                default_explicit = default is not None and (
                    configured_default is not UNSET or self.default_factory is not None
                )
                object.__setattr__(tag, "_default_explicit", default_explicit)
                self._tag_cache[addr] = tag
        # _tag_cache is typed dict[int, LiveTag]; both assignment sites store a
        # LiveTag, so no runtime cast is needed here (this runs on every access).
        return self._tag_cache[addr]

    def _new_tag_for_slot(
        self,
        addr: int,
        *,
        retentive: bool,
        default: Any,
        comment: str,
        choices: ChoiceMap | None,
        readonly: bool,
        external: bool,
        final: bool,
        public: bool,
        lock: bool,
        physical: Physical | None = None,
        link: str | None = None,
        min: int | float | None = None,
        max: int | float | None = None,
        uom: str | None = None,
    ) -> LiveTag:
        tag = LiveTag(
            name=self._effective_slot_name(addr),
            type=self.type,
            retentive=retentive,
            default=default,
            comment=comment,
            choices=choices,
            readonly=readonly,
            external=external,
            final=final,
            public=public,
            lock=lock,
            physical=physical,
            link=link,
            min=min,
            max=max,
            uom=uom,
        )
        return self._annotate_tag(tag, addr)

    def _annotate_tag(self, tag: LiveTag, addr: int) -> LiveTag:
        object.__setattr__(tag, "_pyrung_block", self)
        object.__setattr__(tag, "_pyrung_block_addr", addr)
        runtime = self._pyrung_structure_runtime
        if runtime is None:
            return tag

        object.__setattr__(tag, "_pyrung_structure_runtime", runtime)
        object.__setattr__(tag, "_pyrung_structure_kind", self._pyrung_structure_kind)
        object.__setattr__(tag, "_pyrung_structure_name", self._pyrung_structure_name)
        object.__setattr__(tag, "_pyrung_structure_field", self._pyrung_structure_field)
        object.__setattr__(tag, "_pyrung_structure_index", addr)
        return tag

    def _type_default(self) -> Any:
        return _TYPE_DEFAULTS.get(self.type, 0)

    def _slot_field(self, addr: int, field_name: str, inherited: Any) -> Any:
        """Effective value for one slot field: override if set, else inherited."""
        entry = self._slot_config.get(addr)
        if entry is None:
            return inherited
        value = getattr(entry, field_name)
        return inherited if value is UNSET else value

    def _effective_slot_policy(self, addr: int) -> tuple[bool, Any]:
        retentive = self._slot_field(addr, "retentive", self.retentive)
        default = self._slot_field(addr, "default", UNSET)
        if default is UNSET:
            mapped = self._mapped_tags.get(addr)
            if mapped is not None and not retentive:
                default = mapped.default
            elif self.default_factory is not None:
                default = self.default_factory(addr)
            else:
                default = self._type_default()
        return retentive, default

    def _effective_slot_comment(self, addr: int) -> str:
        comment = self._slot_field(addr, "comment", UNSET)
        if comment is not UNSET:
            return comment
        mapped = self._mapped_tags.get(addr)
        if mapped is not None and mapped.comment:
            return mapped.comment
        return ""

    def _effective_slot_hints(self, addr: int) -> _SlotHints:
        mapped = self._mapped_tags.get(addr)
        if mapped is not None:
            m_choices = mapped.choices
            m_min = mapped.min
            m_max = mapped.max
            m_uom = mapped.uom
        else:
            m_choices = self._pyrung_field_choices
            m_min = self._pyrung_field_min
            m_max = self._pyrung_field_max
            m_uom = self._pyrung_field_uom
        return _SlotHints(
            self._slot_field(addr, "choices", m_choices),
            self._slot_field(addr, "readonly", self._pyrung_field_readonly),
            self._slot_field(addr, "external", self._pyrung_field_external),
            self._slot_field(addr, "final", self._pyrung_field_final),
            self._slot_field(addr, "public", self._pyrung_field_public),
            self._slot_field(addr, "lock", self._pyrung_field_lock),
            self._slot_field(addr, "physical", self._pyrung_field_physical),
            self._slot_field(addr, "link", self._pyrung_field_link),
            self._slot_field(addr, "min", m_min),
            self._slot_field(addr, "max", m_max),
            self._slot_field(addr, "uom", m_uom),
        )

    def _assert_not_materialized(self, addr: int, *, action: str) -> None:
        if addr in self._tag_cache:
            raise ValueError(
                f"Cannot {action} {self.name}[{addr}] after tag materialization. "
                "Configure slot metadata before reading/indexing that slot."
            )

    def _apply_slot_override(
        self,
        addr: int,
        field_name: str,
        new_value: Any,
    ) -> None:
        entry = self._slot_config.get(addr)
        if entry is None:
            entry = SlotConfig()
            self._slot_config[addr] = entry
        existing = getattr(entry, field_name)
        if existing is not UNSET:
            if existing == new_value:
                return
            raise ValueError(
                f"Slot {self.name}[{addr}] {field_name} conflict: "
                f"already set to {existing!r}, cannot change to {new_value!r}."
            )
        setattr(entry, field_name, new_value)
        if addr in self._tag_cache:
            object.__setattr__(self._tag_cache[addr], field_name, new_value)

    def _configured_addresses(self) -> set[int]:
        """Addresses with at least one slot override or mapped tag."""
        return set(self._slot_config) | set(self._mapped_tags)

    def reset(self) -> None:
        """Clear all slot overrides, cached tags, and mapped-tag registrations."""
        self._tag_cache.clear()
        self._slot_config.clear()
        self._mapped_tags.clear()
        self._hw_alias.clear()

    @overload
    def slot(self, addr: int) -> SlotView: ...

    @overload
    def slot(
        self,
        addr: int,
        *,
        name: str = ...,
        retentive: bool = ...,
        default: Any = ...,
        comment: str = ...,
        choices: ChoiceMap | None = ...,
        readonly: bool = ...,
        external: bool = ...,
        final: bool = ...,
        public: bool = ...,
        physical: Physical | None = ...,
        link: str | None = ...,
        min: int | float | None = ...,
        max: int | float | None = ...,
        uom: str | None = ...,
    ) -> SlotView: ...

    @overload
    def slot(self, addr: int, end: int) -> RangeSlotView: ...

    @overload
    def slot(
        self,
        addr: int,
        end: int,
        *,
        retentive: bool = ...,
        default: Any = ...,
    ) -> RangeSlotView: ...

    def slot(
        self,
        addr: int,
        end: int | None = None,
        *,
        name: object = UNSET,
        retentive: bool | None = None,
        default: object = UNSET,
        comment: object = UNSET,
        choices: object = UNSET,
        readonly: object = UNSET,
        external: object = UNSET,
        final: object = UNSET,
        public: object = UNSET,
        lock: object = UNSET,
        physical: object = UNSET,
        link: object = UNSET,
        min: object = UNSET,
        max: object = UNSET,
        uom: object = UNSET,
    ) -> SlotView | RangeSlotView:
        """Inspect, configure, or reset one or more block slots.

        Single slot::

            ds.slot(10)                                # inspect
            ds.slot(10, name="Speed", retentive=True)  # configure
            ds.slot(10).reset()                        # clear overrides

        Range::

            ds.slot(20, 30, retentive=True)            # configure range
            ds.slot(20, 30).reset()                    # clear range

        Args:
            addr: Slot address (always required).
            end: If given, defines an inclusive range ``[addr, end]``.
            name: Custom tag name (single-slot only).
            retentive: Retentive policy override.
            default: Default value override.
            comment: Comment override (empty string clears).

        Returns:
            `SlotView` for a single slot, `RangeSlotView` for a range.
        """
        if end is not None:
            return self._slot_range(addr, end, retentive=retentive, default=default)

        self._validate_address(addr)

        has_config = (
            name is not UNSET
            or retentive is not None
            or default is not UNSET
            or comment is not UNSET
            or choices is not UNSET
            or readonly is not UNSET
            or external is not UNSET
            or final is not UNSET
            or public is not UNSET
            or lock is not UNSET
            or physical is not UNSET
            or link is not UNSET
            or min is not UNSET
            or max is not UNSET
            or uom is not UNSET
        )
        if has_config:
            if name is not UNSET:
                if not isinstance(name, str):
                    raise TypeError(f"name must be a string, got {type(name).__name__}.")
                self._apply_slot_override(addr, "name", name)
            if retentive is not None:
                self._apply_slot_override(addr, "retentive", bool(retentive))
            if default is not UNSET:
                self._apply_slot_override(addr, "default", default)
            if comment is not UNSET:
                if not isinstance(comment, str):
                    raise TypeError(f"comment must be a string, got {type(comment).__name__}.")
                self._apply_slot_override(addr, "comment", comment)
            if choices is not UNSET:
                normalized = _normalize_choices(
                    choices,
                    tag_type=self.type,
                    owner=f"{self.name}.slot({addr}) choices",
                )
                self._apply_slot_override(addr, "choices", normalized)
            if readonly is not UNSET:
                self._apply_slot_override(addr, "readonly", bool(readonly))
            if external is not UNSET:
                self._apply_slot_override(addr, "external", bool(external))
            if final is not UNSET:
                self._apply_slot_override(addr, "final", bool(final))
            if public is not UNSET:
                self._apply_slot_override(addr, "public", bool(public))
            if lock is not UNSET:
                self._apply_slot_override(addr, "lock", bool(lock))
            if physical is not UNSET:
                self._apply_slot_override(addr, "physical", cast(Physical | None, physical))
            if link is not UNSET:
                if link is not None and not isinstance(link, str):
                    raise TypeError(f"link must be a string or None, got {type(link).__name__}.")
                self._apply_slot_override(addr, "link", link)
            if min is not UNSET:
                if min is not None and not isinstance(min, (int, float)):
                    raise TypeError(f"min must be numeric or None, got {type(min).__name__}.")
                self._apply_slot_override(addr, "min", min)
            if max is not UNSET:
                if max is not None and not isinstance(max, (int, float)):
                    raise TypeError(f"max must be numeric or None, got {type(max).__name__}.")
                self._apply_slot_override(addr, "max", max)
            if uom is not UNSET:
                if uom is not None and not isinstance(uom, str):
                    raise TypeError(f"uom must be a string or None, got {type(uom).__name__}.")
                self._apply_slot_override(addr, "uom", uom)

        return SlotView(self, addr)

    def _slot_range(
        self,
        start: int,
        end: int,
        *,
        retentive: bool | None = None,
        default: object = UNSET,
    ) -> RangeSlotView:
        if start > end:
            raise ValueError(
                f"slot range start ({start}) must be <= end ({end}) for {self.name} block"
            )
        self._validate_window_bound(start, "Start")
        self._validate_window_bound(end, "End")

        has_config = retentive is not None or default is not UNSET
        if has_config:
            addresses = self._window_addresses(start, end)
            for addr in addresses:
                if retentive is not None:
                    self._apply_slot_override(addr, "retentive", bool(retentive))
                if default is not UNSET:
                    self._apply_slot_override(addr, "default", default)

        return RangeSlotView(self, start, end)

    def _effective_slot_name(self, addr: int) -> str:
        name = self._slot_field(addr, "name", UNSET)
        if name is not UNSET:
            return name
        mapped = self._mapped_tags.get(addr)
        if mapped is not None:
            return mapped.name
        return self._format_tag_name(addr)

    def _format_tag_name(self, addr: int) -> str:
        if self.address_formatter is None:
            return f"{self.name}{addr}"
        return self.address_formatter(self.name, addr)

    def _is_sparse_address_valid(self, addr: int) -> bool:
        if self.valid_ranges is None:
            return True
        return any(lo <= addr <= hi for lo, hi in self.valid_ranges)

    def _validate_address(self, addr: int) -> None:
        if addr < self.start or addr > self.end:
            raise IndexError(
                f"Address {addr} out of range for {self.name} block "
                f"(valid: {self.start}-{self.end})"
            )
        if not self._is_sparse_address_valid(addr):
            raise IndexError(
                f"Address {addr} is not valid for {self.name} block "
                f"(valid window: {self.start}-{self.end}, sparse-ranged)"
            )

    def _validate_window_bound(self, addr: int, label: str) -> None:
        if addr < self.start or addr > self.end:
            raise IndexError(
                f"{label} address {addr} out of range for {self.name} block "
                f"(valid: {self.start}-{self.end})"
            )

    def _window_addresses(self, start: int, end: int) -> range | tuple[int, ...]:
        if self.valid_ranges is None:
            return range(start, end + 1)

        addresses: set[int] = set()
        for lo, hi in self.valid_ranges:
            if hi < start or lo > end:
                continue
            seg_start = max(start, lo)
            seg_end = min(end, hi)
            addresses.update(range(seg_start, seg_end + 1))
        return tuple(sorted(addresses))

    @overload
    def select(self, start: int, end: int) -> BlockRange: ...

    @overload
    def select(
        self, start: Tag | Expression, end: int | Tag | Expression
    ) -> IndirectBlockRange: ...

    @overload
    def select(self, start: int, end: Tag | Expression) -> IndirectBlockRange: ...

    def select(
        self, start: int | Tag | Any, end: int | Tag | Any
    ) -> BlockRange | IndirectBlockRange:
        """Select an inclusive range of addresses for block operations.

        Both `start` and `end` are **inclusive**: ``DS.select(1, 10)`` yields
        10 tags (1, 2, … 10).  This mirrors the block constructor convention and
        avoids the off-by-one confusion of Python's half-open slices.

        For sparse blocks (`valid_ranges` set), returns only the valid addresses
        within the window — gaps are silently skipped.

        Args:
            start: Start address. ``int`` for a static range; `Tag` or
                `Expression` for a dynamically-resolved range.
            end: End address. ``int`` for a static range; `Tag` or
                `Expression` for a dynamically-resolved range.

        Returns:
            `BlockRange` when both arguments are ``int`` (resolved at
            definition time). `IndirectBlockRange` when either argument is a
            `Tag` or `Expression` (resolved each scan at execution time).

        Raises:
            ValueError: If ``start > end``.
            IndexError: If either bound is outside the block's ``[start, end]``.

        Example:
            ```python
            # Static range
            DS.select(1, 100)              # BlockRange, DS1..DS100

            # Sparse window (Click X bank)
            x.select(1, 21)               # valid tags only: X001..X016, X021

            # Dynamic range (resolved each scan)
            DS.select(start_tag, end_tag)  # IndirectBlockRange

            # Use with bulk instructions:
            fill(0, DS.select(1, 10))
            blockcopy(DS.select(1, 10), DD.select(1, 10))
            search(">=", 100, DS.select(1, 100), result=Found, found=FoundFlag)
            ```
        """

        if isinstance(start, int) and isinstance(end, int):
            if start > end:
                raise ValueError(
                    f"select start ({start}) must be <= end ({end}) for {self.name} block"
                )
            self._validate_window_bound(start, "Start")
            self._validate_window_bound(end, "End")
            return BlockRange(self, start, end)
        else:
            return IndirectBlockRange(self, start, end)

    def map_to(self, target: BlockRange) -> MappingEntry:
        """Create a logical-to-hardware mapping entry.

        Each logical slot is registered as the canonical occupant of its
        hardware address, so ``bank[addr]`` — including an indirect
        ``bank[expr]`` read — returns the logical tag and sees its default.
        This is the block-range counterpart of `Tag.map_to`; without it a
        block-mapped register exists as two disconnected tags and the
        indirect reader sees a blank 0 instead of the configured value.
        """
        from pyrung.core.tag import MappingEntry

        logical_addresses = tuple(self.select(self.start, self.end).addresses)
        hardware_addresses = tuple(target.addresses)
        if len(logical_addresses) <= len(hardware_addresses):
            hw_block = target.block
            for logical_addr, hw_addr in zip(logical_addresses, hardware_addresses, strict=False):
                existing = hw_block._mapped_tags.get(hw_addr)
                slot_tag = self[logical_addr]
                if existing is not None and existing.name != slot_tag.name:
                    # An overlapping claim — leave the incumbent in place and let
                    # TagMap report it as a hardware address conflict.
                    continue
                hw_block._mapped_tags[hw_addr] = slot_tag
                hw_block._tag_cache.pop(hw_addr, None)
                self._hw_alias[logical_addr] = (hw_block, hw_addr)

        return MappingEntry(source=self, target=target)

    def __repr__(self) -> str:
        return f"Block({self.name!r}, {self.type}, {self.start}, {self.end})"


@dataclass(eq=False)
class InputBlock(Block):
    """Factory for creating `InputTag` instances from a physical input memory region.

    `InputBlock` is identical to `Block` except:

    - Indexing returns `LiveInputTag` (not `LiveTag`), so elements have `.immediate`.
    - Always non-retentive — physical inputs do not survive power cycles.

    Use `InputBlock` when the tags represent real hardware inputs (sensors,
    switches, etc.). In simulation, values are supplied via `runner.patch()` or
    `runner.force()` during the *Read Inputs* scan phase.

    Example:
        ```python
        X = InputBlock("X", TagType.BOOL, 1, 16)
        X[1]           # → LiveInputTag("X1", BOOL)
        X[1].immediate # → ImmediateRef — bypass image table
        X.select(1, 8) # → BlockRange for bulk operations
        ```
    """

    def __init__(
        self,
        name: str,
        type: TagType,
        start: int,
        end: int,
        valid_ranges: tuple[tuple[int, int], ...] | None = None,
        address_formatter: Callable[[str, int], str] | None = None,
        default_factory: Callable[[int], Any] | None = None,
    ):
        super().__init__(
            name=name,
            type=type,
            start=start,
            end=end,
            retentive=False,
            valid_ranges=valid_ranges,
            address_formatter=address_formatter,
            default_factory=default_factory,
        )

    @overload
    def __getitem__(self, key: int) -> LiveInputTag: ...

    @overload
    def __getitem__(self, key: slice) -> Never: ...

    @overload
    def __getitem__(self, key: Tag) -> IndirectRef: ...

    @overload
    def __getitem__(self, key: Expression) -> IndirectExprRef: ...

    @overload
    def __getitem__(self, key: object) -> LiveInputTag | IndirectRef | IndirectExprRef: ...

    def __getitem__(
        self, key: int | slice | Tag | Any
    ) -> LiveInputTag | IndirectRef | IndirectExprRef:
        return cast(LiveInputTag | IndirectRef | IndirectExprRef, super().__getitem__(key))

    def _new_tag_for_slot(
        self,
        addr: int,
        *,
        retentive: bool,
        default: Any,
        comment: str,
        choices: ChoiceMap | None,
        readonly: bool,
        external: bool,
        final: bool,
        public: bool,
        lock: bool,
        physical: Physical | None = None,
        link: str | None = None,
        min: int | float | None = None,
        max: int | float | None = None,
        uom: str | None = None,
    ) -> LiveInputTag:
        tag = LiveInputTag(
            name=self._effective_slot_name(addr),
            type=self.type,
            retentive=retentive,
            default=default,
            comment=comment,
            choices=choices,
            readonly=readonly,
            external=external,
            final=final,
            public=public,
            lock=lock,
            physical=physical,
            link=link,
            min=min,
            max=max,
            uom=uom,
        )
        return cast(LiveInputTag, self._annotate_tag(tag, addr))

    def _get_tag(self, addr: int) -> LiveInputTag:
        return cast(LiveInputTag, super()._get_tag(addr))


@dataclass(eq=False)
class OutputBlock(Block):
    """Factory for creating `OutputTag` instances from a physical output memory region.

    `OutputBlock` is identical to `Block` except:

    - Indexing returns `LiveOutputTag` (not `LiveTag`), so elements have `.immediate`.
    - Always non-retentive — physical outputs do not survive power cycles.

    Writes to `OutputTag` elements are immediately visible to subsequent rungs
    within the same scan (standard PLC behavior). The actual hardware write
    happens at the *Write Outputs* scan phase (phase 6).

    Example:
        ```python
        Y = OutputBlock("Y", TagType.BOOL, 1, 16)
        Y[1]           # → LiveOutputTag("Y1", BOOL)
        Y[1].immediate # → ImmediateRef — bypass image table
        Y.select(1, 8) # → BlockRange for bulk operations
        ```
    """

    def __init__(
        self,
        name: str,
        type: TagType,
        start: int,
        end: int,
        valid_ranges: tuple[tuple[int, int], ...] | None = None,
        address_formatter: Callable[[str, int], str] | None = None,
        default_factory: Callable[[int], Any] | None = None,
    ):
        super().__init__(
            name=name,
            type=type,
            start=start,
            end=end,
            retentive=False,
            valid_ranges=valid_ranges,
            address_formatter=address_formatter,
            default_factory=default_factory,
        )

    @overload
    def __getitem__(self, key: int) -> LiveOutputTag: ...

    @overload
    def __getitem__(self, key: slice) -> Never: ...

    @overload
    def __getitem__(self, key: Tag) -> IndirectRef: ...

    @overload
    def __getitem__(self, key: Expression) -> IndirectExprRef: ...

    @overload
    def __getitem__(self, key: object) -> LiveOutputTag | IndirectRef | IndirectExprRef: ...

    def __getitem__(
        self, key: int | slice | Tag | Any
    ) -> LiveOutputTag | IndirectRef | IndirectExprRef:
        return cast(LiveOutputTag | IndirectRef | IndirectExprRef, super().__getitem__(key))

    def _new_tag_for_slot(
        self,
        addr: int,
        *,
        retentive: bool,
        default: Any,
        comment: str,
        choices: ChoiceMap | None,
        readonly: bool,
        external: bool,
        final: bool,
        public: bool,
        lock: bool,
        physical: Physical | None = None,
        link: str | None = None,
        min: int | float | None = None,
        max: int | float | None = None,
        uom: str | None = None,
    ) -> LiveOutputTag:
        tag = LiveOutputTag(
            name=self._effective_slot_name(addr),
            type=self.type,
            retentive=retentive,
            default=default,
            comment=comment,
            choices=choices,
            readonly=readonly,
            external=external,
            final=final,
            public=public,
            lock=lock,
            physical=physical,
            link=link,
            min=min,
            max=max,
            uom=uom,
        )
        return cast(LiveOutputTag, self._annotate_tag(tag, addr))

    def _get_tag(self, addr: int) -> LiveOutputTag:
        return cast(LiveOutputTag, super()._get_tag(addr))


@dataclass(frozen=True)
class BlockRange:
    """Contiguous range of addresses for block operations.

    Attributes:
        block: Source Block.
        start: Starting address (inclusive).
        end: Ending address (inclusive).
    """

    block: Block
    start: int
    end: int
    reverse_order: bool = False

    @property
    def addresses(self) -> range | tuple[int, ...]:
        """Return addresses in this block window, filtered by block rules."""
        addresses = self.block._window_addresses(self.start, self.end)
        if not self.reverse_order:
            return addresses
        if isinstance(addresses, range):
            return range(addresses.stop - 1, addresses.start - 1, -1)
        return tuple(reversed(addresses))

    def tags(self) -> list[Tag]:
        """Return list of Tag objects for all addresses in this block."""
        return [self.block._get_tag(addr) for addr in self.addresses]

    def reverse(self) -> BlockRange:
        """Return this same window with address iteration reversed."""
        return BlockRange(self.block, self.start, self.end, not self.reverse_order)

    def sum(self) -> SumExpr:
        """Return a SumExpr that evaluates to the sum of all tag values in this range."""
        from pyrung.core.expression import SumExpr

        return SumExpr(self)

    def __len__(self) -> int:
        return len(self.addresses)

    def __iter__(self) -> Iterator[Tag]:
        """Iterate over Tags in this block."""
        for addr in self.addresses:
            yield self.block._get_tag(addr)

    def __eq__(self, other: object) -> RangeComparison:  # ty: ignore[invalid-method-override]
        """Create equality comparison for search."""
        return RangeComparison(self, "==", other)

    def __ne__(self, other: object) -> RangeComparison:  # ty: ignore[invalid-method-override]
        """Create inequality comparison for search."""
        return RangeComparison(self, "!=", other)

    def __lt__(self, other: Any) -> RangeComparison:
        """Create less-than comparison for search."""
        return RangeComparison(self, "<", other)

    def __le__(self, other: Any) -> RangeComparison:
        """Create less-than-or-equal comparison for search."""
        return RangeComparison(self, "<=", other)

    def __gt__(self, other: Any) -> RangeComparison:
        """Create greater-than comparison for search."""
        return RangeComparison(self, ">", other)

    def __ge__(self, other: Any) -> RangeComparison:
        """Create greater-than-or-equal comparison for search."""
        return RangeComparison(self, ">=", other)

    def __hash__(self) -> int:
        return hash((id(self.block), self.start, self.end, self.reverse_order))

    def __repr__(self) -> str:
        return f"BlockRange({self.block.name}[{self.start}:{self.end}])"


@dataclass(frozen=True)
class IndirectBlockRange:
    """Memory block with runtime-resolved bounds.

    Wraps a Block with start/end that may be Tags or Expressions,
    resolved at scan time.

    Attributes:
        block: Source Block.
        start_expr: Start address (int, Tag, or Expression).
        end_expr: End address (int, Tag, or Expression).
    """

    block: Block
    start_expr: int | Tag | Any
    end_expr: int | Tag | Any
    reverse_order: bool = False

    def resolve_ctx(self, ctx: ScanContext | ConditionView) -> BlockRange:
        """Resolve expressions to concrete BlockRange using ScanContext."""
        start = self._resolve_one(self.start_expr, ctx)
        end = self._resolve_one(self.end_expr, ctx)
        resolved = self.block.select(start, end)
        if not isinstance(resolved, BlockRange):
            raise TypeError("Resolved indirect block range did not produce BlockRange")
        return resolved.reverse() if self.reverse_order else resolved

    def reverse(self) -> IndirectBlockRange:
        """Return this same dynamic window with address iteration reversed."""
        return IndirectBlockRange(
            block=self.block,
            start_expr=self.start_expr,
            end_expr=self.end_expr,
            reverse_order=not self.reverse_order,
        )

    def __eq__(self, other: object) -> RangeComparison:  # ty: ignore[invalid-method-override]
        """Create equality comparison for search."""
        return RangeComparison(self, "==", other)

    def __ne__(self, other: object) -> RangeComparison:  # ty: ignore[invalid-method-override]
        """Create inequality comparison for search."""
        return RangeComparison(self, "!=", other)

    def __lt__(self, other: Any) -> RangeComparison:
        """Create less-than comparison for search."""
        return RangeComparison(self, "<", other)

    def __le__(self, other: Any) -> RangeComparison:
        """Create less-than-or-equal comparison for search."""
        return RangeComparison(self, "<=", other)

    def __gt__(self, other: Any) -> RangeComparison:
        """Create greater-than comparison for search."""
        return RangeComparison(self, ">", other)

    def __ge__(self, other: Any) -> RangeComparison:
        """Create greater-than-or-equal comparison for search."""
        return RangeComparison(self, ">=", other)

    def __hash__(self) -> int:
        return hash((id(self.block), self.reverse_order))

    @staticmethod
    def _resolve_one(expr: int | Tag | Any, ctx: ScanContext | ConditionView) -> int:
        from pyrung.core.expression import Expression

        if isinstance(expr, int):
            return expr
        if isinstance(expr, Expression):
            return int(expr.evaluate(ctx))
        if isinstance(expr, Tag):
            return int(ctx.get_tag(expr.name, expr.default))
        raise TypeError(f"Cannot resolve {type(expr).__name__} to address")


@dataclass(frozen=True)
class RangeComparison:
    """Comparison expression over a block range, used by ``search()``.

    Created by applying a comparison operator to a ``.select()`` result::

        DS.select(1, 100) >= 100   # RangeComparison(range, ">=", 100)
        Txt.select(1, 50) == "A"   # RangeComparison(range, "==", "A")
    """

    search_range: BlockRange | IndirectBlockRange
    operator: str
    value: Any


@dataclass(frozen=True)
class IndirectRef:
    """Tag with runtime-resolved address via pointer.

    IndirectRef wraps a Block and pointer Tag. The actual
    address is resolved from the pointer's value at scan time.

    Attributes:
        block: Block to index into.
        pointer: Tag whose value determines the address.
    """

    block: Block
    pointer: Tag

    def resolve(self, state: SystemState) -> Tag:
        """Resolve pointer value to concrete Tag.

        Args:
            state: Current system state to read pointer value from.

        Returns:
            Concrete Tag at the resolved address.

        Raises:
            IndexError: If resolved address is out of range.
        """
        ptr_value = state.tags.get(self.pointer.name, self.pointer.default)
        self.block._validate_address(ptr_value)
        return self.block._get_tag(ptr_value)

    def resolve_ctx(self, ctx: ScanContext | ConditionView) -> Tag:
        """Resolve pointer value to concrete Tag using ScanContext.

        Args:
            ctx: ScanContext to read pointer value from.

        Returns:
            Concrete Tag at the resolved address.

        Raises:
            IndexError: If resolved address is out of range.
        """
        ptr_value = ctx.get_tag(self.pointer.name, self.pointer.default)
        self.block._validate_address(ptr_value)
        return self.block._get_tag(ptr_value)

    def __eq__(self, other: object) -> Condition:  # ty: ignore[invalid-method-override]
        """Create equality comparison condition."""
        from pyrung.core.condition import IndirectCompareEq

        return IndirectCompareEq(self, other)

    def __ne__(self, other: object) -> Condition:  # ty: ignore[invalid-method-override]
        """Create inequality comparison condition."""
        from pyrung.core.condition import IndirectCompareNe

        return IndirectCompareNe(self, other)

    def __lt__(self, other: Any) -> IndirectCompareLt:
        """Create less-than comparison condition."""
        from pyrung.core.condition import IndirectCompareLt

        return IndirectCompareLt(self, other)

    def __le__(self, other: Any) -> IndirectCompareLe:
        """Create less-than-or-equal comparison condition."""
        from pyrung.core.condition import IndirectCompareLe

        return IndirectCompareLe(self, other)

    def __gt__(self, other: Any) -> IndirectCompareGt:
        """Create greater-than comparison condition."""
        from pyrung.core.condition import IndirectCompareGt

        return IndirectCompareGt(self, other)

    def __ge__(self, other: Any) -> IndirectCompareGe:
        """Create greater-than-or-equal comparison condition."""
        from pyrung.core.condition import IndirectCompareGe

        return IndirectCompareGe(self, other)

    def __hash__(self) -> int:
        return hash((id(self.block), self.pointer.name))

    def __repr__(self) -> str:
        return f"IndirectRef({self.block.name}[{self.pointer.name}])"


@dataclass(frozen=True)
class IndirectExprRef:
    """Tag with runtime-resolved address via expression.

    IndirectExprRef wraps a Block and an Expression. The actual
    address is computed from the expression at scan time.

    This enables pointer arithmetic like DS[idx + 1] where idx is a Tag.

    Attributes:
        block: Block to index into.
        expr: Expression whose value determines the address.
    """

    block: Block
    expr: Any  # Expression type - use Any to avoid circular import

    def resolve_ctx(self, ctx: ScanContext | ConditionView) -> Tag:
        """Resolve expression value to concrete Tag using ScanContext.

        Args:
            ctx: ScanContext to evaluate expression against.

        Returns:
            Concrete Tag at the computed address.

        Raises:
            IndexError: If resolved address is out of range.
        """
        addr = int(self.expr.evaluate(ctx))
        self.block._validate_address(addr)
        return self.block._get_tag(addr)

    def __repr__(self) -> str:
        return f"IndirectExprRef({self.block.name}[{self.expr}])"


# ---------------------------------------------------------------------------
# Typed block convenience constructors
# ---------------------------------------------------------------------------


class _TypedBlockBase(Block):
    """Base for typed block convenience constructors.

    Mirrors the scalar tag pattern: type is baked into the class,
    name is inferred from the assignment target when omitted.
    """

    _block_type: TagType
    _default_retentive: bool

    def __init__(
        self,
        start: int,
        end: int,
        *,
        name: str | None = None,
        retentive: bool | None = None,
        valid_ranges: tuple[tuple[int, int], ...] | None = None,
        address_formatter: Callable[[str, int], str] | None = None,
        default_factory: Callable[[int], Any] | None = None,
    ):
        import sys

        from pyrung.core._naming import _resolve_name

        name = _resolve_name(type(self).__name__, name, sys._getframe(1))
        if retentive is None:
            retentive = self._default_retentive
        super().__init__(
            name=name,
            type=self._block_type,
            start=start,
            end=end,
            retentive=retentive,
            valid_ranges=valid_ranges,
            address_formatter=address_formatter,
            default_factory=default_factory,
        )


class BoolBlock(_TypedBlockBase):
    _block_type = TagType.BOOL
    _default_retentive = False


class IntBlock(_TypedBlockBase):
    _block_type = TagType.INT
    _default_retentive = True


class DintBlock(_TypedBlockBase):
    _block_type = TagType.DINT
    _default_retentive = True


class RealBlock(_TypedBlockBase):
    _block_type = TagType.REAL
    _default_retentive = True


class WordBlock(_TypedBlockBase):
    _block_type = TagType.WORD
    _default_retentive = True


class CharBlock(_TypedBlockBase):
    _block_type = TagType.CHAR
    _default_retentive = True
