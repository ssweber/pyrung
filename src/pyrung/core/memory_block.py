"""Block-based memory regions and indirect addressing.

Block provides factory methods for creating Tags from typed memory regions.
IndirectRef enables pointer/indirect addressing resolved at runtime.
BlockRange represents contiguous ranges for block operations.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Final, Never, cast, overload

from pyrung.core.tag import (
    LiveInputTag,
    LiveOutputTag,
    LiveTag,
    Tag,
    TagType,
)

if TYPE_CHECKING:
    from pyrung.core.condition import (
        Condition,
        IndirectCompareGe,
        IndirectCompareGt,
        IndirectCompareLe,
        IndirectCompareLt,
    )
    from pyrung.core.context import ScanContext
    from pyrung.core.expression import Expression
    from pyrung.core.state import SystemState
    from pyrung.core.tag import MappingEntry


UNSET: Final = object()


@dataclass(frozen=True)
class SlotConfig:
    """Effective runtime policy for one block slot."""

    retentive: bool
    default: Any
    retentive_overridden: bool
    default_overridden: bool


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
    _tag_cache: dict[int, Tag] = field(default_factory=dict, repr=False)
    _slot_retentive_overrides: dict[int, bool] = field(default_factory=dict, repr=False)
    _slot_default_overrides: dict[int, Any] = field(default_factory=dict, repr=False)

    def __post_init__(self):
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
            retentive, default = self._effective_slot_policy(addr)
            self._tag_cache[addr] = self._new_tag_for_slot(addr, retentive=retentive, default=default)
        return cast(LiveTag, self._tag_cache[addr])

    def _new_tag_for_slot(self, addr: int, *, retentive: bool, default: Any) -> LiveTag:
        return LiveTag(
            name=self._format_tag_name(addr),
            type=self.type,
            retentive=retentive,
            default=default,
        )

    def _type_default(self) -> Any:
        defaults = {
            TagType.BOOL: False,
            TagType.INT: 0,
            TagType.DINT: 0,
            TagType.REAL: 0.0,
            TagType.WORD: 0,
            TagType.CHAR: "",
        }
        return defaults.get(self.type, 0)

    def _effective_slot_policy(self, addr: int) -> tuple[bool, Any]:
        retentive = self._slot_retentive_overrides.get(addr, self.retentive)
        if addr in self._slot_default_overrides:
            default = self._slot_default_overrides[addr]
        elif self.default_factory is not None:
            default = self.default_factory(addr)
        else:
            default = self._type_default()
        return retentive, default

    def _assert_not_materialized(self, addr: int, *, action: str) -> None:
        if addr in self._tag_cache:
            raise ValueError(
                f"Cannot {action} {self.name}[{addr}] after tag materialization. "
                "Configure slot policy before reading/indexing that slot."
            )

    def configure_slot(
        self,
        addr: int,
        *,
        retentive: bool | None = None,
        default: object = UNSET,
    ) -> None:
        """Set per-slot runtime policy before this slot is materialized."""
        self._validate_address(addr)
        self._assert_not_materialized(addr, action="configure slot policy for")

        if retentive is not None:
            self._slot_retentive_overrides[addr] = bool(retentive)
        if default is not UNSET:
            self._slot_default_overrides[addr] = default

    def configure_range(
        self,
        start: int,
        end: int,
        *,
        retentive: bool | None = None,
        default: object = UNSET,
    ) -> None:
        """Set per-slot policy for all valid addresses in the inclusive window."""
        if start > end:
            raise ValueError(
                f"configure_range start ({start}) must be <= end ({end}) for {self.name} block"
            )
        self._validate_window_bound(start, "Start")
        self._validate_window_bound(end, "End")

        addresses = self._window_addresses(start, end)
        for addr in addresses:
            self._assert_not_materialized(addr, action="configure slot policy for")

        for addr in addresses:
            if retentive is not None:
                self._slot_retentive_overrides[addr] = bool(retentive)
            if default is not UNSET:
                self._slot_default_overrides[addr] = default

    def clear_slot_config(self, addr: int) -> None:
        """Clear per-slot policy overrides for one address."""
        self._validate_address(addr)
        self._assert_not_materialized(addr, action="clear slot policy for")
        self._slot_retentive_overrides.pop(addr, None)
        self._slot_default_overrides.pop(addr, None)

    def clear_range_config(self, start: int, end: int) -> None:
        """Clear per-slot policy overrides for all valid addresses in a window."""
        if start > end:
            raise ValueError(
                f"clear_range_config start ({start}) must be <= end ({end}) for {self.name} block"
            )
        self._validate_window_bound(start, "Start")
        self._validate_window_bound(end, "End")

        addresses = self._window_addresses(start, end)
        for addr in addresses:
            self._assert_not_materialized(addr, action="clear slot policy for")

        for addr in addresses:
            self._slot_retentive_overrides.pop(addr, None)
            self._slot_default_overrides.pop(addr, None)

    def slot_config(self, addr: int) -> SlotConfig:
        """Return the effective runtime slot policy without materializing a Tag."""
        self._validate_address(addr)
        retentive, default = self._effective_slot_policy(addr)
        return SlotConfig(
            retentive=retentive,
            default=default,
            retentive_overridden=addr in self._slot_retentive_overrides,
            default_overridden=addr in self._slot_default_overrides,
        )

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
        """Create a logical-to-hardware mapping entry."""
        from pyrung.core.tag import MappingEntry

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
    `runner.add_force()` during the *Read Inputs* scan phase.

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

    def _new_tag_for_slot(self, addr: int, *, retentive: bool, default: Any) -> LiveInputTag:
        return LiveInputTag(
            name=self._format_tag_name(addr),
            type=self.type,
            retentive=retentive,
            default=default,
        )

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

    def _new_tag_for_slot(self, addr: int, *, retentive: bool, default: Any) -> LiveOutputTag:
        return LiveOutputTag(
            name=self._format_tag_name(addr),
            type=self.type,
            retentive=retentive,
            default=default,
        )

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

    def as_value(self) -> Any:
        """Wrap this range for TXT->numeric character-value conversion."""
        from pyrung.core.copy_modifiers import as_value

        return as_value(self)

    def as_ascii(self) -> Any:
        """Wrap this range for TXT->numeric ASCII-code conversion."""
        from pyrung.core.copy_modifiers import as_ascii

        return as_ascii(self)

    def __len__(self) -> int:
        return len(self.addresses)

    def __iter__(self) -> Iterator[Tag]:
        """Iterate over Tags in this block."""
        for addr in self.addresses:
            yield self.block._get_tag(addr)

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

    def resolve_ctx(self, ctx: ScanContext) -> BlockRange:
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

    def as_value(self) -> Any:
        """Wrap this range for TXT->numeric character-value conversion."""
        from pyrung.core.copy_modifiers import as_value

        return as_value(self)

    def as_ascii(self) -> Any:
        """Wrap this range for TXT->numeric ASCII-code conversion."""
        from pyrung.core.copy_modifiers import as_ascii

        return as_ascii(self)

    @staticmethod
    def _resolve_one(expr: int | Tag | Any, ctx: ScanContext) -> int:
        from pyrung.core.expression import Expression

        if isinstance(expr, int):
            return expr
        if isinstance(expr, Expression):
            return int(expr.evaluate(ctx))
        if isinstance(expr, Tag):
            return int(ctx.get_tag(expr.name, expr.default))
        raise TypeError(f"Cannot resolve {type(expr).__name__} to address")


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

    def resolve_ctx(self, ctx: ScanContext) -> Tag:
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

    def __eq__(self, other: object) -> Condition:  # type: ignore[override]
        """Create equality comparison condition."""
        from pyrung.core.condition import IndirectCompareEq

        return IndirectCompareEq(self, other)

    def __ne__(self, other: object) -> Condition:  # type: ignore[override]
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

    def resolve_ctx(self, ctx: ScanContext) -> Tag:
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
