"""Block-based memory regions and indirect addressing.

Block provides factory methods for creating Tags from typed memory regions.
IndirectRef enables pointer/indirect addressing resolved at runtime.
BlockRange represents contiguous ranges for block operations.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Never, cast, overload

from pyrung.core.tag import InputTag, OutputTag, Tag, TagType

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


@dataclass
class Block:
    """Factory for creating Tags from a typed memory region.

    Block defines a named region of memory with:
    - Consistent type for all addresses
    - Inclusive address bounds [start, end]
    - Default retentive behavior

    Attributes:
        name: Block prefix (e.g., "DS", "DD", "C").
        type: TagType for all tags in this block.
        start: Inclusive lower bound address.
        end: Inclusive upper bound address.
        retentive: Default retentive setting for tags. Default False.
    """

    name: str
    type: TagType
    start: int
    end: int
    retentive: bool = False
    valid_ranges: tuple[tuple[int, int], ...] | None = None
    address_formatter: Callable[[str, int], str] | None = None
    _tag_cache: dict[int, Tag] = field(default_factory=dict, repr=False)

    def __post_init__(self):
        if self.start < 1:
            raise ValueError(f"start must be >= 1, got {self.start}")
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
    def __getitem__(self, key: int) -> Tag: ...

    @overload
    def __getitem__(self, key: slice) -> Never: ...

    @overload
    def __getitem__(self, key: Tag) -> IndirectRef: ...

    @overload
    def __getitem__(self, key: Expression) -> IndirectExprRef: ...

    @overload
    def __getitem__(self, key: object) -> Tag | IndirectRef | IndirectExprRef: ...

    def __getitem__(self, key: int | slice | Tag | Any) -> Tag | IndirectRef | IndirectExprRef:
        """Access tags by address, pointer tag, or expression.

        Args:
            key: Address (int), pointer tag (Tag), or expression for computed address.

        Returns:
            - int: Single Tag (cached)
            - Tag: IndirectRef for pointer addressing
            - Expression: IndirectExprRef for computed address (e.g., DS[idx + 1])
            - slice: raises TypeError

        Raises:
            IndexError: If int address is 0 or out of range.
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

    def _get_tag(self, addr: int) -> Tag:
        """Get or create a Tag for the given address."""
        if addr not in self._tag_cache:
            self._tag_cache[addr] = Tag(
                name=self._format_tag_name(addr),
                type=self.type,
                retentive=self.retentive,
            )
        return self._tag_cache[addr]

    def _format_tag_name(self, addr: int) -> str:
        if self.address_formatter is None:
            return f"{self.name}{addr}"
        return self.address_formatter(self.name, addr)

    def _is_sparse_address_valid(self, addr: int) -> bool:
        if self.valid_ranges is None:
            return True
        return any(lo <= addr <= hi for lo, hi in self.valid_ranges)

    def _validate_address(self, addr: int) -> None:
        if addr == 0:
            raise IndexError("Address 0 is not valid; addresses start at 1")
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
        if addr == 0:
            raise IndexError("Address 0 is not valid; addresses start at 1")
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
        """Select a range of addresses (inclusive bounds).

        Args:
            start: Start address (int, Tag, or Expression).
            end: End address (int, Tag, or Expression).

        Returns:
            BlockRange if both are ints, IndirectBlockRange otherwise.
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

    def __repr__(self) -> str:
        return f"Block({self.name!r}, {self.type}, {self.start}, {self.end})"


@dataclass
class InputBlock(Block):
    """Block that creates InputTag instances for physical inputs.

    InputBlock always has retentive=False (inputs are not retentive).
    """

    def __init__(
        self,
        name: str,
        type: TagType,
        start: int,
        end: int,
        valid_ranges: tuple[tuple[int, int], ...] | None = None,
        address_formatter: Callable[[str, int], str] | None = None,
    ):
        super().__init__(
            name=name,
            type=type,
            start=start,
            end=end,
            retentive=False,
            valid_ranges=valid_ranges,
            address_formatter=address_formatter,
        )

    @overload
    def __getitem__(self, key: int) -> InputTag: ...

    @overload
    def __getitem__(self, key: slice) -> Never: ...

    @overload
    def __getitem__(self, key: Tag) -> IndirectRef: ...

    @overload
    def __getitem__(self, key: Expression) -> IndirectExprRef: ...

    @overload
    def __getitem__(self, key: object) -> InputTag | IndirectRef | IndirectExprRef: ...

    def __getitem__(self, key: int | slice | Tag | Any) -> InputTag | IndirectRef | IndirectExprRef:
        return cast(InputTag | IndirectRef | IndirectExprRef, super().__getitem__(key))

    def _get_tag(self, addr: int) -> InputTag:
        """Get or create an InputTag for the given address."""
        if addr not in self._tag_cache:
            self._tag_cache[addr] = InputTag(
                name=self._format_tag_name(addr),
                type=self.type,
                retentive=False,
            )
        return cast(InputTag, self._tag_cache[addr])


@dataclass
class OutputBlock(Block):
    """Block that creates OutputTag instances for physical outputs.

    OutputBlock always has retentive=False (outputs are not retentive).
    """

    def __init__(
        self,
        name: str,
        type: TagType,
        start: int,
        end: int,
        valid_ranges: tuple[tuple[int, int], ...] | None = None,
        address_formatter: Callable[[str, int], str] | None = None,
    ):
        super().__init__(
            name=name,
            type=type,
            start=start,
            end=end,
            retentive=False,
            valid_ranges=valid_ranges,
            address_formatter=address_formatter,
        )

    @overload
    def __getitem__(self, key: int) -> OutputTag: ...

    @overload
    def __getitem__(self, key: slice) -> Never: ...

    @overload
    def __getitem__(self, key: Tag) -> IndirectRef: ...

    @overload
    def __getitem__(self, key: Expression) -> IndirectExprRef: ...

    @overload
    def __getitem__(self, key: object) -> OutputTag | IndirectRef | IndirectExprRef: ...

    def __getitem__(
        self, key: int | slice | Tag | Any
    ) -> OutputTag | IndirectRef | IndirectExprRef:
        return cast(OutputTag | IndirectRef | IndirectExprRef, super().__getitem__(key))

    def _get_tag(self, addr: int) -> OutputTag:
        """Get or create an OutputTag for the given address."""
        if addr not in self._tag_cache:
            self._tag_cache[addr] = OutputTag(
                name=self._format_tag_name(addr),
                type=self.type,
                retentive=False,
            )
        return cast(OutputTag, self._tag_cache[addr])


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
