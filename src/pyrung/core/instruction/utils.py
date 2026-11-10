"""Shared helpers for instruction execution and validation."""

from __future__ import annotations

from collections.abc import Callable, Iterable
from functools import wraps
from typing import TYPE_CHECKING, Any, TypeVar, cast

from pyrung.core.time_mode import dt_to_ns

F = TypeVar("F", bound=Callable[..., Any])

if TYPE_CHECKING:
    from fractions import Fraction

    from pyrung.core.context import ConditionView, ScanContext
    from pyrung.core.tag import Tag, TagType
    from pyrung.core.time_mode import TimeUnit


def elapsed_units(carry: Any, dt: float, unit: TimeUnit) -> tuple[int, int | Fraction]:
    """This scan's *dt*, added to what ``_frac:<acc>`` carries: whole units and what is left.

    The carry is the nanoseconds since the accumulator's last whole unit, so no time is lost
    however many scans it takes to make a unit.  It is a fraction of a unit (a float) in
    memory saved before carries were nanoseconds.
    """
    if carry.__class__ is float:
        carry = round(carry * unit.ns)
    return divmod(carry + dt_to_ns(dt), unit.ns)


def guard_oneshot_execution(func: F) -> F:
    """Skip execute calls when one-shot state says not to run.

    An instruction that runs settles the flags in its ``_fault_follows`` as it
    starts, before it reads anything: it clears them, and a raise in its body
    sets them again.  A source naming one of those flags reads it off.  One
    with ``_fault_settles_idle`` (a Math) settles them on a scan it does not run
    too: its rung false, or a one-shot that does not fire (CLICK,
    ``flag_steps``, ``flag_reads/oneshot_held``).  Any other leaves them then.
    """

    @wraps(func)
    def wrapper(self: Any, ctx: ScanContext, enabled: bool, *args: Any, **kwargs: Any) -> Any:
        if not enabled:
            if getattr(self, "_oneshot", False):
                ctx.set_memory(self.memory_key("_oneshot"), False)
            if self._fault_settles_idle:
                _settle_fault_flags(self, ctx)
            return None
        if getattr(self, "_oneshot", False):
            key = self.memory_key("_oneshot")
            if ctx.get_memory(key, False):
                if self._fault_settles_idle:
                    _settle_fault_flags(self, ctx)
                return None
            ctx.set_memory(key, True)
        _settle_fault_flags(self, ctx)
        return func(self, ctx, enabled, *args, **kwargs)

    return cast(F, wrapper)


def _settle_fault_flags(instr: Any, ctx: ScanContext) -> None:
    for name in instr._fault_follows:
        if ctx._get_tag_internal(name, False):
            ctx._set_tag_internal(name, False)


def to_condition(obj: Any) -> Any:
    """Normalize instruction condition input(s) to a single Condition."""
    from pyrung.core.condition import BitCondition, Condition, _normalize_and_condition
    from pyrung.core.tag import Tag as TagClass
    from pyrung.core.tag import TagType

    if obj is None:
        return None

    def _coerce(value: object) -> Condition:
        if isinstance(value, Condition):
            return value
        if isinstance(value, TagClass):
            if value.type == TagType.BOOL:
                return BitCondition(value)
            raise TypeError(
                f"Non-BOOL tag '{value.name}' cannot be used directly as condition. "
                "Use comparison operators: tag == value, tag > 0, etc."
            )
        raise TypeError(f"Expected Condition or Tag, got {type(value)}")

    return _normalize_and_condition(
        obj,
        coerce=_coerce,
        empty_error="condition requires at least one condition",
        group_empty_error="condition group cannot be empty",
    )


def instruction_condition_view(ctx: ScanContext, instr: Any) -> ConditionView:
    """The condition view box *instr* reads its helper pins from, taken at box entry.

    A box reads its enable and all its pins at one moment, as it starts and
    before any write of its own, whether or not it is enabled: the view is
    frozen there, so no pin sees the box's own Done/Acc/step writes, and it
    sees every write of the rows above the box.  The box's edge pins are
    scanned (their memory stored) as the view is taken.  Call it before the
    box writes anything.
    """
    from pyrung.core.site_memory import build_box_frame, scan_edge_frame

    view = ctx._new_condition_view()
    frame = instr._edge_frame
    if frame is None:
        frame = build_box_frame(instr)
    if frame:
        scan_edge_frame(ctx, view, frame)
    return view


def resolve_preset_ctx(preset: Tag | int, ctx: ScanContext) -> int:
    """Resolve preset to int value (supports Tag or literal)."""
    from pyrung.core.tag import Tag as TagClass

    if isinstance(preset, TagClass):
        return ctx.get_tag(preset.name, preset.default)
    return preset


def validate_preset(preset: object, *, label: str) -> None:
    """Reject presets that are not an int literal or an INT, DINT or WORD tag.

    A REAL preset has no single comparison every backend agrees on, and CLICK
    refuses one anyway (DS timer presets, DS/DD counter presets).
    """
    from pyrung.core.tag import Tag as TagClass
    from pyrung.core.tag import TagType

    allowed = (TagType.INT, TagType.DINT, TagType.WORD)
    if isinstance(preset, TagClass):
        assert_tag_type(preset, allowed, label=label, include_tag_name=True)
        return
    if isinstance(preset, bool) or not isinstance(preset, int):
        raise TypeError(
            f"{label} must be an int or an {_allowed_type_text(allowed)} tag; "
            f"got {type(preset).__name__}"
        )


def _allowed_type_text(allowed_types: Iterable[TagType]) -> str:
    names = [tag_type.name for tag_type in allowed_types]
    if len(names) == 1:
        return names[0]
    if len(names) == 2:
        return f"{names[0]} or {names[1]}"
    return ", ".join(names[:-1]) + f", or {names[-1]}"


def assert_tag_type(
    tag: Tag,
    allowed_types: Iterable[TagType],
    *,
    label: str,
    include_tag_name: bool = False,
) -> None:
    """Assert a single tag is one of the allowed types."""
    allowed = tuple(allowed_types)
    if tag.type in allowed:
        return
    suffix = f" at {tag.name}" if include_tag_name else ""
    raise TypeError(f"{label} must be {_allowed_type_text(allowed)}; got {tag.type.name}{suffix}")
