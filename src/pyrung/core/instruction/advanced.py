"""Automatically generated module split."""

from __future__ import annotations

from dataclasses import dataclass
from operator import eq, ge, gt, le, lt, ne
from typing import TYPE_CHECKING, Any, assert_never

from pyrung.core.crossing import Constraint, Eq
from pyrung.core.tag import Tag, TagType

from .advance import (
    AdvanceProfile,
    AdvanceStep,
    ConditionDemand,
    Snapshot,
    constraint_holds,
)
from .base import Instruction, OneShotMixin
from .data_transfer import _read
from .operands import (
    RANGE,
    TAG,
    ConstArg,
    ExprArg,
    PointerArg,
    PointerRangeArg,
    RangeArg,
    RangeOperand,
    Span,
    TagArg,
    Value,
    parse_value,
)
from .resolvers import _set_fault_address_error, reads_indexed_element
from .utils import (
    guard_oneshot_execution,
    instruction_condition_view,
    to_condition,
)

if TYPE_CHECKING:
    from collections.abc import Callable

    from pyrung.core.context import ScanContext
    from pyrung.core.memory_block import BlockRange, IndirectBlockRange, RangeComparison

_SEARCH_OPERATOR_MAP = {
    "==": eq,
    "!=": ne,
    "<": lt,
    "<=": le,
    ">": gt,
    ">=": ge,
}


# search() writes only into a tag, so a pointer refusal says how to reach one.
_POINTER_HINT = "Then copy() that tag to the pointer."


@dataclass(frozen=True)
class _SearchRange(RangeOperand):
    """The range of a search().  The author writes it inside the comparison."""

    def refusal(self, call: str, arg: str, value: Any) -> Exception | None:
        """Return the error that refuses *value*, or None if it fits.

        The error names the comparison, because the range has no argument of its own.
        """
        if self._accepts(value):
            return None
        return TypeError(
            f"{call} comparison must use a .select() range, got {type(value).__name__}."
        )


_FOUND = TAG.of(TagType.BOOL, pointers=False, immediate=False, pointer_hint=_POINTER_HINT)
_RESULT = TAG.of(
    TagType.INT, TagType.DINT, pointers=False, immediate=False, pointer_hint=_POINTER_HINT
)
_SEARCH_RANGE = _SearchRange(immediate=False)
_BIT_RANGE = RANGE.of(TagType.BOOL, immediate=False)


class SearchInstruction(OneShotMixin, Instruction):
    """Scan a block range for the first element matching a condition (SRCH).

    Walks the tags in `search_range` in address order and writes the address
    of the first match to `result`, or −1 on miss.  `found` is True on hit,
    False on miss.

    **Numeric search** — uses the selected comparison operator against a
    constant or tag value.

    **Text search** — `search_range` must be a CHAR block range.  The search
    value is treated as a substring; only ``"=="`` and ``"!="`` are valid
    operators.  A window of ``len(value)`` consecutive CHAR tags is compared.

    **Continuous mode** — when `continuous=True`, the search resumes from the
    address after the previous result rather than restarting each scan.
    Set ``result = 0`` to restart, or set ``result = -1`` to signal exhaustion.
    As on a CLICK, a result that is not an address inside the range (below,
    above, or any other value) is a miss, ``-1``.

    Args:
        comparison: A `RangeComparison` from a block-range comparison
            expression, e.g. ``DS.select(1, 100) >= 100``.
        result: INT or DINT tag to receive the matched address (or −1).
        found: BOOL tag set True on hit, False on miss.
        continuous: Resume from previous result position. Default False.
        oneshot: Execute only on rung rising edge. Default False.
    """

    _reads = ("value", "search_range")
    _writes = ("result", "found")
    _conditions = ()
    _structural_fields = (
        "condition",
        "continuous",
    )  # condition is RangeComparison, not a Condition tree
    _call_operands = (("comparison", ("search_range", "value")),)
    _call_keyword_operands = (("result", "result"), ("found", "found"))
    _call_keywords = ("continuous", "oneshot")

    def _call_argument(self, name: str, text: Callable[[Any], str]) -> str:
        """Return the comparison as the call writes it, such as ``DS1..DS10 >= 100``."""
        return f"{text(self.search_range)} {self.condition} {text(self.value)}"

    # The table leaves out the range.  The author writes it inside the comparison, so it
    # has no argument of its own.
    _operands = {"found": _FOUND, "result": _RESULT}

    def __init__(
        self,
        comparison: RangeComparison,
        *,
        result: Tag,
        found: Tag,
        continuous: bool = False,
        oneshot: bool = False,
    ):
        from pyrung.core.memory_block import RangeComparison

        if not isinstance(comparison, RangeComparison):
            raise TypeError(
                "search() expects a comparison expression, "
                f"e.g. DS.select(1, 10) >= 100, got {type(comparison).__name__}"
            )
        self.found: TagArg = _FOUND.parse_tag("search()", "found", found)
        self.result: TagArg = _RESULT.parse_tag("search()", "result", result)
        if comparison.operator not in _SEARCH_OPERATOR_MAP:
            raise ValueError(
                f"Invalid search condition: {comparison.operator!r}. "
                "Expected one of: ==, !=, <, <=, >, >="
            )
        self.search_range: Span = _SEARCH_RANGE.parse(
            "search()", "search_range", comparison.search_range
        )
        # The builder does not check the value.  The scan raises for a value that it
        # cannot compare.
        self.value: Value | Span = parse_value(comparison.value)

        OneShotMixin.__init__(self, oneshot)
        self.condition = comparison.operator
        if isinstance(self.search_range, PointerRangeArg) or reads_indexed_element(
            comparison.value
        ):
            # A window out of its block, or a value read below it, sets SC44.  The
            # analyses (PDG, static facts) see the flag written where it can be.
            self._fault_writes = ("fault.address_error",)
        self.continuous = continuous
        self._compare = _SEARCH_OPERATOR_MAP[comparison.operator]

    @guard_oneshot_execution
    def execute(self, ctx: ScanContext, enabled: bool) -> None:
        search_range = self.search_range
        match search_range:
            case RangeArg(span=span):
                resolved_range = span
            case PointerRangeArg(span=span):
                try:
                    resolved_range = span.resolve_ctx(ctx)
                except IndexError:
                    _set_fault_address_error(ctx)
                    return
            case _:
                assert_never(search_range)
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

        text_path = self._is_text_path(tags)
        try:
            rhs = self._read_value(ctx)
        except IndexError:
            _set_fault_address_error(ctx)
            return

        if text_path:
            matched_address = self._search_text(
                tags=tags,
                addresses=addresses,
                cursor_index=cursor_index,
                rhs=rhs,
                ctx=ctx,
            )
        else:
            matched_address = self._search_numeric(
                tags=tags,
                addresses=addresses,
                cursor_index=cursor_index,
                rhs=rhs,
                ctx=ctx,
            )

        if matched_address is None:
            self._write_miss(ctx)
            return

        ctx.set_tags({self.result.tag.name: matched_address, self.found.tag.name: True})

    def _read_value(self, ctx: ScanContext) -> Any:
        """Return the value that the search compares on this scan."""
        value = self.value
        match value:
            case TagArg() | PointerArg() | ExprArg() | ConstArg():
                return _read(value, ctx)
            case RangeArg(span=span) | PointerRangeArg(span=span):
                # The search compares each cell with the range object itself.
                return span
            case _:
                assert_never(value)

    def _write_miss(self, ctx: ScanContext) -> None:
        ctx.set_tags({self.result.tag.name: -1, self.found.tag.name: False})

    def _resolve_cursor_index(
        self, addresses: list[int], reverse_order: bool, ctx: ScanContext
    ) -> int | None:
        if not self.continuous:
            return 0

        result = self.result.tag
        current_result = int(ctx.get_tag(result.name, result.default))
        if current_result == 0:
            return 0
        if current_result == -1:
            return None

        # CLICK: an old result inside the search range resumes after it.  Any other
        # value (below, above, negative) is a miss.  A result on the last address
        # has nothing after it, so that is a miss too.
        for idx, addr in enumerate(addresses):
            if addr == current_result:
                return idx + 1 if idx + 1 < len(addresses) else None
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
        self, tags: list[Tag], addresses: list[int], cursor_index: int, rhs: Any, ctx: ScanContext
    ) -> int | None:
        for idx in range(cursor_index, len(tags)):
            candidate = ctx.get_tag(tags[idx].name, tags[idx].default)
            if self._compare(candidate, rhs):
                return addresses[idx]
        return None

    def _search_text(
        self, tags: list[Tag], addresses: list[int], cursor_index: int, rhs: Any, ctx: ScanContext
    ) -> int | None:
        if self.condition not in {"==", "!="}:
            raise ValueError("Text search only supports '==' and '!=' conditions")

        rhs_text = str(rhs)
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
    """Shift register — shift a BOOL range on a clock edge (SHR/SHL).

    On each rising edge of the clock condition, shifts all bits one position
    and inserts the rung's combined condition (the data bit) at position [0].

    - **Data bit** — the combined rung condition at execution time.
    - **Clock** — shift fires on OFF→ON edge (rising edge, not level).
    - **Reset** — level-sensitive: clears all bits in the range to False
      while True.

    Direction is determined by the range order:

    - ``C.select(1, 8)`` → shifts low-to-high (data at C1, exits at C8).
    - ``C.select(8, 1).reverse()`` → shifts high-to-low.

    The `bit_range` must contain only BOOL tags.

    Args:
        bit_range: `BlockRange` or `IndirectBlockRange` from `.select()`.
        data_condition: Combined rung condition (injected by DSL).
        clock_condition: Tag or condition whose rising edge triggers the shift.
        reset_condition: Tag or condition that clears all bits (level-sensitive).
    """

    ALWAYS_EXECUTES = True
    INERT_WHEN_DISABLED = False
    _reads = ("bit_range",)
    _writes = ("bit_range",)
    _conditions = ("data_condition", "clock_condition", "reset_condition")
    _structural_fields = ()
    _operands = {"bit_range": _BIT_RANGE}
    _call_operands = (("bit_range", "bit_range"),)
    _call_keywords = ()
    _call_chained = (("clock", "clock_condition"), ("reset", "reset_condition"))

    def __init__(
        self,
        bit_range: BlockRange | IndirectBlockRange,
        data_condition: Any,
        clock_condition: Any,
        reset_condition: Any,
    ):
        self.bit_range: Span = _BIT_RANGE.parse("shift()", "bit_range", bit_range)
        self.data_condition = to_condition(data_condition)
        self.clock_condition = to_condition(clock_condition)
        self.reset_condition = to_condition(reset_condition)

        if self.clock_condition is None:
            raise ValueError("shift requires a clock condition")
        if self.reset_condition is None:
            raise ValueError("shift requires a reset condition")

    def _resolve_tags(self, ctx: ScanContext) -> list[Tag]:
        bit_range = self.bit_range
        match bit_range:
            case RangeArg(span=span):
                tags = span.tags()
            case PointerRangeArg(span=span):
                tags = span.resolve_ctx(ctx).tags()
            case _:
                assert_never(bit_range)
        if not tags:
            raise ValueError("shift bit_range resolved to an empty range")
        return tags

    def execute(self, ctx: ScanContext, enabled: bool) -> None:
        tags = self._resolve_tags(ctx)
        condition_view = instruction_condition_view(ctx, self)

        clock_curr = bool(self.clock_condition.evaluate(condition_view))
        clock_prev = bool(ctx.get_memory(self.memory_key("_shift_prev_clock"), False))
        reset_active = bool(self.reset_condition.evaluate(condition_view))
        rising_edge = clock_curr and not clock_prev

        if rising_edge:
            prev_values = [bool(ctx.get_tag(tag.name, tag.default)) for tag in tags]
            updates = {tags[0].name: bool(enabled)}
            for idx, tag in enumerate(tags[1:], start=1):
                updates[tag.name] = prev_values[idx - 1]
            ctx.set_tags(updates)

        if reset_active:
            ctx.set_tags({tag.name: False for tag in tags})

        ctx.set_memory(self.memory_key("_shift_prev_clock"), clock_curr)

    def has_pins(self) -> bool:
        return True

    def advance_profile(self) -> AdvanceProfile | None:
        # An indirect range has no fixed set of owned channels at index-build
        # time. Registering the whole block would claim channels this instance
        # may not write, so leave it to ordinary trace.
        bit_range = self.bit_range
        match bit_range:
            case RangeArg(span=span):
                tags = tuple(span.tags())
            case PointerRangeArg():
                return None
            case _:
                assert_never(bit_range)

        def plan(constraint: Constraint, snapshot: Snapshot) -> AdvanceStep | None:
            if not isinstance(constraint, Eq) or len(constraint.values) != 1:
                return None
            if constraint_holds(constraint, snapshot) is True:
                return None
            desired = next(iter(constraint.values))
            if not isinstance(desired, bool):
                return None
            target_index = next(
                (index for index, tag in enumerate(tags) if tag.name == constraint.tag),
                None,
            )
            if target_index is None:
                return None

            # One clock can extend a matching prefix by one cell. Establish the
            # injected data and a safe reset level, pulse once, then retrace.
            boundary = tags[target_index]
            for tag in tags[: target_index + 1]:
                if bool(snapshot.get(tag.name, tag.default)) is not desired:
                    boundary = tag
                    break
            return AdvanceStep(
                until=Eq(boundary.name, frozenset((desired,))),
                holds=(
                    ConditionDemand(self.data_condition, desired),
                    ConditionDemand(self.reset_condition, False),
                ),
                pulse=ConditionDemand(self.clock_condition),
            )

        return AdvanceProfile(channels=tags, plan=plan)
