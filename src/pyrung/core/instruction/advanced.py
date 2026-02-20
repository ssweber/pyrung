"""Automatically generated module split."""

from __future__ import annotations

from operator import eq, ge, gt, le, lt, ne
from typing import TYPE_CHECKING, Any

from pyrung.core.tag import Tag

from .base import Instruction, OneShotMixin
from .resolvers import (
    resolve_block_range_ctx,
    resolve_block_range_tags_ctx,
    resolve_tag_or_value_ctx,
)

if TYPE_CHECKING:
    from pyrung.core.context import ScanContext
    from pyrung.core.memory_block import BlockRange, IndirectBlockRange

_SEARCH_OPERATOR_MAP = {
    "==": eq,
    "!=": ne,
    "<": lt,
    "<=": le,
    ">": gt,
    ">=": ge,
}


class SearchInstruction(OneShotMixin, Instruction):
    """Search instruction.

    Scans a selected range for the first value (or text window) matching
    the given condition and writes:
    - result: matched address, or -1 on miss
    - found: True on hit, False on miss
    """

    def __init__(
        self,
        condition: str,
        value: Any,
        search_range: BlockRange | IndirectBlockRange,
        result: Tag,
        found: Tag,
        continuous: bool = False,
        oneshot: bool = False,
    ):
        from pyrung.core.memory_block import BlockRange, IndirectBlockRange
        from pyrung.core.tag import TagType

        if condition not in _SEARCH_OPERATOR_MAP:
            raise ValueError(
                f"Invalid search condition: {condition!r}. Expected one of: ==, !=, <, <=, >, >="
            )
        if not isinstance(search_range, (BlockRange, IndirectBlockRange)):
            raise TypeError(
                "search_range must be BlockRange or IndirectBlockRange from .select(), "
                f"got {type(search_range).__name__}"
            )
        if found.type != TagType.BOOL:
            raise TypeError(f"search found tag must be BOOL, got {found.type.name}")
        if result.type not in {TagType.INT, TagType.DINT}:
            raise TypeError(f"search result tag must be INT or DINT, got {result.type.name}")

        OneShotMixin.__init__(self, oneshot)
        self.condition = condition
        self.value = value
        self.search_range = search_range
        self.result = result
        self.found = found
        self.continuous = continuous
        self._compare = _SEARCH_OPERATOR_MAP[condition]

    def execute(self, ctx: ScanContext, enabled: bool) -> None:
        if not self.should_execute(enabled):
            return

        resolved_range = resolve_block_range_ctx(self.search_range, ctx)
        addresses = list(resolved_range.addresses)
        tags = resolved_range.tags()

        if not addresses:
            self._write_miss(ctx)
            return

        cursor_index = self._resolve_cursor_index(
            addresses=addresses,
            reverse_order=resolved_range.reverse_order,
            ctx=ctx,
        )
        if cursor_index is None:
            self._write_miss(ctx)
            return

        if self._is_text_path(tags):
            matched_address = self._search_text(
                tags=tags,
                addresses=addresses,
                cursor_index=cursor_index,
                ctx=ctx,
            )
        else:
            matched_address = self._search_numeric(
                tags=tags,
                addresses=addresses,
                cursor_index=cursor_index,
                ctx=ctx,
            )

        if matched_address is None:
            self._write_miss(ctx)
            return

        ctx.set_tags({self.result.name: matched_address, self.found.name: True})

    def _write_miss(self, ctx: ScanContext) -> None:
        ctx.set_tags({self.result.name: -1, self.found.name: False})

    def _resolve_cursor_index(
        self, addresses: list[int], reverse_order: bool, ctx: ScanContext
    ) -> int | None:
        if not self.continuous:
            return 0

        current_result = int(ctx.get_tag(self.result.name, self.result.default))
        if current_result == 0:
            return 0
        if current_result == -1:
            return None

        if reverse_order:
            for idx, addr in enumerate(addresses):
                if addr < current_result:
                    return idx
            return None

        for idx, addr in enumerate(addresses):
            if addr > current_result:
                return idx
        return None

    def _is_text_path(self, tags: list[Tag]) -> bool:
        from pyrung.core.tag import TagType

        first_type = tags[0].type
        if first_type == TagType.CHAR:
            for tag in tags:
                if tag.type != TagType.CHAR:
                    raise TypeError(
                        "search text ranges must contain only CHAR tags; "
                        f"got {tag.type.name} at {tag.name}"
                    )
            return True

        if first_type in {TagType.INT, TagType.DINT, TagType.REAL, TagType.WORD}:
            return False

        raise TypeError(
            "search range tags must be INT, DINT, REAL, WORD, or CHAR; "
            f"got {first_type.name} at {tags[0].name}"
        )

    def _search_numeric(
        self, tags: list[Tag], addresses: list[int], cursor_index: int, ctx: ScanContext
    ) -> int | None:
        rhs_value = resolve_tag_or_value_ctx(self.value, ctx)

        for idx in range(cursor_index, len(tags)):
            candidate = ctx.get_tag(tags[idx].name, tags[idx].default)
            if self._compare(candidate, rhs_value):
                return addresses[idx]
        return None

    def _search_text(
        self, tags: list[Tag], addresses: list[int], cursor_index: int, ctx: ScanContext
    ) -> int | None:
        if self.condition not in {"==", "!="}:
            raise ValueError("Text search only supports '==' and '!=' conditions")

        rhs_text = str(resolve_tag_or_value_ctx(self.value, ctx))
        if rhs_text == "":
            raise ValueError("Text search value cannot be empty")

        window_len = len(rhs_text)
        if window_len > len(tags):
            return None

        last_start = len(tags) - window_len
        if cursor_index > last_start:
            return None

        for start in range(cursor_index, last_start + 1):
            candidate = "".join(
                str(ctx.get_tag(tags[start + offset].name, tags[start + offset].default))
                for offset in range(window_len)
            )
            if self.condition == "==" and candidate == rhs_text:
                return addresses[start]
            if self.condition == "!=" and candidate != rhs_text:
                return addresses[start]
        return None


class ShiftInstruction(Instruction):
    """Shift register instruction.

    Terminal instruction that always executes and checks:
    - data condition (rung combined condition) for inserted bit value
    - clock condition for OFF->ON edge shift trigger
    - reset condition (level) to clear all bits in the range
    """

    def __init__(
        self,
        bit_range: BlockRange | IndirectBlockRange,
        data_condition: Any,
        clock_condition: Any,
        reset_condition: Any,
    ):
        from pyrung.core.memory_block import BlockRange, IndirectBlockRange

        if not isinstance(bit_range, (BlockRange, IndirectBlockRange)):
            raise TypeError(
                f"shift bit_range must be BlockRange or IndirectBlockRange, "
                f"got {type(bit_range).__name__}"
            )

        self.bit_range = bit_range
        self.data_condition = self._to_condition(data_condition)
        self.clock_condition = self._to_condition(clock_condition)
        self.reset_condition = self._to_condition(reset_condition)
        self._prev_clock_key = f"_shift_prev_clock:{id(self)}"

        if self.clock_condition is None:
            raise ValueError("shift requires a clock condition")
        if self.reset_condition is None:
            raise ValueError("shift requires a reset condition")

    def _to_condition(self, obj: Any) -> Any:
        """Convert a BOOL tag to BitCondition for condition inputs."""
        from pyrung.core.condition import BitCondition
        from pyrung.core.tag import Tag as TagClass
        from pyrung.core.tag import TagType

        if obj is None:
            return None
        if isinstance(obj, TagClass):
            if obj.type == TagType.BOOL:
                return BitCondition(obj)
            raise TypeError(
                f"Non-BOOL tag '{obj.name}' cannot be used directly as condition. "
                "Use comparison operators: tag == value, tag > 0, etc."
            )
        return obj

    def _resolve_tags(self, ctx: ScanContext) -> list[Tag]:
        from pyrung.core.tag import TagType

        tags = resolve_block_range_tags_ctx(self.bit_range, ctx)
        if not tags:
            raise ValueError("shift bit_range resolved to an empty range")
        for tag in tags:
            if tag.type != TagType.BOOL:
                raise TypeError(
                    f"shift bit_range must contain only BOOL tags; "
                    f"got {tag.type.name} at {tag.name}"
                )
        return tags

    def always_execute(self) -> bool:
        """Shift must always run to capture clock edges while rung is false."""
        return True

    def execute(self, ctx: ScanContext, enabled: bool) -> None:
        tags = self._resolve_tags(ctx)

        data_bit = enabled
        clock_curr = bool(self.clock_condition.evaluate(ctx))
        clock_prev = bool(ctx.get_memory(self._prev_clock_key, False))
        rising_edge = clock_curr and not clock_prev

        if rising_edge:
            prev_values = [bool(ctx.get_tag(tag.name, tag.default)) for tag in tags]
            updates = {tags[0].name: bool(data_bit)}
            for idx, tag in enumerate(tags[1:], start=1):
                updates[tag.name] = prev_values[idx - 1]
            ctx.set_tags(updates)

        reset_active = bool(self.reset_condition.evaluate(ctx))
        if reset_active:
            ctx.set_tags({tag.name: False for tag in tags})

        ctx.set_memory(self._prev_clock_key, clock_curr)

    def is_inert_when_disabled(self) -> bool:
        return False
