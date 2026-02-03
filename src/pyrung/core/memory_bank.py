"""Memory bank and indirect addressing for typed PLC memory regions.

MemoryBank provides factory methods for creating Tags from typed memory regions.
IndirectTag enables pointer/indirect addressing resolved at runtime.
MemoryBlock represents contiguous ranges for block operations.
"""

from __future__ import annotations

import warnings
from collections.abc import Iterator
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from pyrung.core.tag import Tag, TagType

if TYPE_CHECKING:
    from pyrung.core.condition import (
        Condition,
        IndirectCompareGe,
        IndirectCompareGt,
        IndirectCompareLe,
        IndirectCompareLt,
    )
    from pyrung.core.context import ScanContext
    from pyrung.core.state import SystemState


@dataclass
class MemoryBank:
    """Factory for creating Tags from a typed memory region.

    MemoryBank defines a named region of PLC memory with:
    - Consistent type for all addresses
    - Valid address range
    - Default retentive behavior
    - Per-address configuration via register()

    Attributes:
        name: Bank prefix (e.g., "DS", "DD", "C").
        tag_type: TagType for all tags in this bank.
        addr_range: Valid address range for this bank.
        retentive: Default retentive setting for tags. Default False.
        input_only: If True, tags can be read but not written (X bank).
        read_only: If True, tags cannot be written at all (SC, SD).
        initial_values: Per-address initial values (non-retentive tags only).
        retentive_exceptions: Addresses that differ from bank's retentive default.
        nicknames: Mapping of tag names to addresses.
    """

    name: str
    tag_type: TagType
    addr_range: range
    retentive: bool = False
    input_only: bool = False
    read_only: bool = False
    initial_values: dict[int, Any] = field(default_factory=dict, repr=False)
    retentive_exceptions: set[int] = field(default_factory=set, repr=False)
    nicknames: dict[str, int] = field(default_factory=dict, repr=False)
    _tag_cache: dict[int, Tag] = field(default_factory=dict, repr=False)

    def register(
        self,
        nickname: str,
        addr: int,
        *,
        initial_value: Any = None,
        retentive: bool | None = None,
    ) -> Tag:
        """Register a tag with a nickname and optional configuration.

        Args:
            nickname: Human-readable name for the tag.
            addr: Address in this memory bank.
            initial_value: Initial value for non-retentive tags.
            retentive: Override bank's default retentive setting.

        Returns:
            The configured Tag.

        Raises:
            ValueError: If address is out of range.

        Note:
            Retentive tags cannot have initial values (they retain across power
            cycles). If both are specified, retentive takes precedence and a
            warning is issued if initial_value is meaningful (not 0 or "").
        """
        if addr not in self.addr_range:
            raise ValueError(
                f"Address {addr} out of range for {self.name} bank "
                f"(valid: {self.addr_range.start}-{self.addr_range.stop - 1})"
            )

        # Determine effective retentive setting
        if retentive is not None:
            is_retentive = retentive
            # Update exceptions set
            if retentive != self.retentive:
                self.retentive_exceptions.add(addr)
            else:
                self.retentive_exceptions.discard(addr)
        else:
            is_retentive = self._is_retentive(addr)

        # Handle initial_value vs retentive conflict
        if is_retentive:
            if initial_value is not None and str(initial_value) not in ("0", ""):
                warnings.warn(
                    f"Tag '{nickname}' at {self.name}{addr} is retentive but has "
                    f"initial_value={initial_value!r}. Retentive takes precedence; "
                    "initial_value will be ignored.",
                    UserWarning,
                    stacklevel=2,
                )
            # Clean up: don't store initial_value for retentive addresses
            self.initial_values.pop(addr, None)
        else:
            if initial_value is not None:
                self.initial_values[addr] = initial_value

        # Register nickname
        self.nicknames[nickname] = addr

        # Clear cache if tag was already created (re-registration)
        self._tag_cache.pop(addr, None)

        return self._get_tag(addr)

    def _is_retentive(self, addr: int) -> bool:
        """Determine if an address is retentive."""
        if addr in self.retentive_exceptions:
            return not self.retentive
        return self.retentive

    def __getitem__(
        self, key: int | str | slice | Tag
    ) -> Tag | MemoryBlock | IndirectTag | IndirectExprTag:
        """Access tags by address, nickname, slice, pointer, or expression.

        Args:
            key: Address (int), nickname (str), address range (slice),
                 pointer tag (Tag), or expression for computed address.

        Returns:
            - int: Single Tag (cached)
            - str: Single Tag looked up by nickname
            - slice: MemoryBlock for block operations
            - Tag: IndirectTag for pointer addressing
            - Expression: IndirectExprTag for computed address (e.g., DS[idx + 1])

        Raises:
            ValueError: If address is out of range.
            KeyError: If nickname is not registered.
            TypeError: If key type is invalid.
        """
        # Import here to avoid circular imports
        from pyrung.core.expression import Expression

        if isinstance(key, int):
            return self._get_tag(key)
        elif isinstance(key, str):
            return self._get_tag_by_nickname(key)
        elif isinstance(key, slice):
            return self._get_block(key)
        elif isinstance(key, Expression):
            return IndirectExprTag(self, key)
        elif isinstance(key, Tag):
            return IndirectTag(self, key)
        else:
            raise TypeError(
                f"Invalid key type: {type(key)}. Expected int, str, slice, Tag, or Expression."
            )

    def _get_tag(self, addr: int) -> Tag:
        """Get or create a Tag for the given address."""
        if addr not in self.addr_range:
            raise ValueError(
                f"Address {addr} out of range for {self.name} bank "
                f"(valid: {self.addr_range.start}-{self.addr_range.stop - 1})"
            )

        if addr not in self._tag_cache:
            is_retentive = self._is_retentive(addr)
            # Only use initial_value for non-retentive tags
            default = self.initial_values.get(addr) if not is_retentive else None
            self._tag_cache[addr] = Tag(
                name=f"{self.name}{addr}",
                type=self.tag_type,
                retentive=is_retentive,
                default=default,
            )
        return self._tag_cache[addr]

    def _get_tag_by_nickname(self, nickname: str) -> Tag:
        """Get a Tag by its registered nickname."""
        if nickname not in self.nicknames:
            raise KeyError(
                f"Nickname '{nickname}' not registered in {self.name} bank. "
                "Use register() to add nicknames or access by address."
            )
        return self._get_tag(self.nicknames[nickname])

    def __getattr__(self, name: str) -> Tag:
        """Access tags by nickname as attributes (e.g., DS.Motor1Speed).

        Args:
            name: Registered nickname.

        Returns:
            Tag at the nickname's address.

        Raises:
            AttributeError: If nickname is not registered.
        """
        # Avoid infinite recursion for internal attributes
        if name.startswith("_") or name in (
            "name",
            "tag_type",
            "addr_range",
            "retentive",
            "input_only",
            "read_only",
            "initial_values",
            "retentive_exceptions",
            "nicknames",
        ):
            raise AttributeError(f"'{type(self).__name__}' object has no attribute '{name}'")
        try:
            return self._get_tag_by_nickname(name)
        except KeyError:
            raise AttributeError(
                f"'{type(self).__name__}' has no nickname '{name}'. "
                "Use register() to add nicknames or access by address."
            ) from None

    def _get_block(self, key: slice) -> MemoryBlock:
        """Create a MemoryBlock for the given slice."""
        start = key.start if key.start is not None else self.addr_range.start
        stop = key.stop if key.stop is not None else self.addr_range.stop

        if start not in self.addr_range:
            raise ValueError(
                f"Start address {start} out of range for {self.name} bank "
                f"(valid: {self.addr_range.start}-{self.addr_range.stop - 1})"
            )
        if stop - 1 not in self.addr_range and stop != self.addr_range.stop:
            raise ValueError(
                f"End address {stop - 1} out of range for {self.name} bank "
                f"(valid: {self.addr_range.start}-{self.addr_range.stop - 1})"
            )

        return MemoryBlock(self, start, stop - start)

    def __repr__(self) -> str:
        return (
            f"MemoryBank({self.name!r}, {self.tag_type}, "
            f"range({self.addr_range.start}, {self.addr_range.stop}))"
        )


@dataclass(frozen=True)
class MemoryBlock:
    """Contiguous range of addresses for block operations.

    MemoryBlock represents a slice of a MemoryBank for use in
    block copy operations (Milestone 7).

    Attributes:
        bank: Source MemoryBank.
        start: Starting address.
        length: Number of addresses in block.
    """

    bank: MemoryBank
    start: int
    length: int

    @property
    def addresses(self) -> range:
        """Return the range of addresses in this block."""
        return range(self.start, self.start + self.length)

    def tags(self) -> list[Tag]:
        """Return list of Tag objects for all addresses in this block."""
        result: list[Tag] = []
        for addr in self.addresses:
            item = self.bank[addr]
            # Type narrowing: we know _get_tag returns Tag for int keys
            if isinstance(item, Tag):
                result.append(item)
        return result

    def __len__(self) -> int:
        return self.length

    def __iter__(self) -> Iterator[Tag]:
        """Iterate over Tags in this block."""
        for addr in self.addresses:
            yield self.bank[addr]

    def __repr__(self) -> str:
        return f"MemoryBlock({self.bank.name}[{self.start}:{self.start + self.length}])"


@dataclass(frozen=True)
class IndirectTag:
    """Tag with runtime-resolved address via pointer.

    IndirectTag wraps a MemoryBank and pointer Tag. The actual
    address is resolved from the pointer's value at scan time.

    Attributes:
        bank: MemoryBank to index into.
        pointer: Tag whose value determines the address.
    """

    bank: MemoryBank
    pointer: Tag

    def resolve(self, state: SystemState) -> Tag:
        """Resolve pointer value to concrete Tag.

        Args:
            state: Current system state to read pointer value from.

        Returns:
            Concrete Tag at the resolved address.

        Raises:
            ValueError: If resolved address is out of range.
        """
        ptr_value = state.tags.get(self.pointer.name, self.pointer.default)
        item = self.bank[ptr_value]
        # Type narrowing: _get_tag returns Tag for int keys
        if isinstance(item, Tag):
            return item
        raise TypeError(f"Expected Tag at address {ptr_value}, got {type(item)}")

    def resolve_ctx(self, ctx: ScanContext) -> Tag:
        """Resolve pointer value to concrete Tag using ScanContext.

        Args:
            ctx: ScanContext to read pointer value from.

        Returns:
            Concrete Tag at the resolved address.

        Raises:
            ValueError: If resolved address is out of range.
        """
        ptr_value = ctx.get_tag(self.pointer.name, self.pointer.default)
        item = self.bank[ptr_value]
        # Type narrowing: _get_tag returns Tag for int keys
        if isinstance(item, Tag):
            return item
        raise TypeError(f"Expected Tag at address {ptr_value}, got {type(item)}")

    def __eq__(self, other: object) -> Condition:
        """Create equality comparison condition."""
        from pyrung.core.condition import IndirectCompareEq

        return IndirectCompareEq(self, other)

    def __ne__(self, other: object) -> Condition:
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
        return hash((id(self.bank), self.pointer.name))

    def __repr__(self) -> str:
        return f"IndirectTag({self.bank.name}[{self.pointer.name}])"


@dataclass(frozen=True)
class IndirectExprTag:
    """Tag with runtime-resolved address via expression.

    IndirectExprTag wraps a MemoryBank and an Expression. The actual
    address is computed from the expression at scan time.

    This enables pointer arithmetic like DS[idx + 1] where idx is a Tag.

    Attributes:
        bank: MemoryBank to index into.
        expr: Expression whose value determines the address.
    """

    bank: MemoryBank
    expr: Any  # Expression type - use Any to avoid circular import

    def resolve_ctx(self, ctx: ScanContext) -> Tag:
        """Resolve expression value to concrete Tag using ScanContext.

        Args:
            ctx: ScanContext to evaluate expression against.

        Returns:
            Concrete Tag at the computed address.

        Raises:
            ValueError: If resolved address is out of range.
        """
        addr = int(self.expr.evaluate(ctx))
        item = self.bank[addr]
        if isinstance(item, Tag):
            return item
        raise TypeError(f"Expected Tag at address {addr}, got {type(item)}")

    def __repr__(self) -> str:
        return f"IndirectExprTag({self.bank.name}[{self.expr}])"
