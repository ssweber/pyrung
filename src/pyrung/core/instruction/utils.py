"""Shared helpers for instruction execution and validation."""

from __future__ import annotations

from collections.abc import Callable, Iterable
from functools import wraps
from typing import TYPE_CHECKING, Any, TypeVar, assert_never, cast

from pyrung.core.time_mode import dt_to_ns

from .operands import ConstArg, Preset, TagArg

F = TypeVar("F", bound=Callable[..., Any])

if TYPE_CHECKING:
    from fractions import Fraction

    from pyrung.core.context import ConditionView, ScanContext
    from pyrung.core.tag import Tag, TagType
    from pyrung.core.time_mode import TimeUnit


def elapsed_units(carry: Any, dt: float, unit: TimeUnit) -> tuple[int, int | Fraction]:
    """Add this scan's *dt* to what ``_frac:<acc>`` carries, and return whole units and the rest.

    The carry is the nanoseconds since the accumulator's last whole unit, so no time is lost
    however many scans it takes to make a unit.  Memory saved before carries were
    nanoseconds holds the carry as a fraction of a unit (a float).
    """
    if carry.__class__ is float:
        carry = round(carry * unit.ns)
    return divmod(carry + dt_to_ns(dt), unit.ns)


def guard_oneshot_execution(func: F) -> F:
    """Skip execute calls when one-shot state says not to run.

    An instruction that runs clears the flags in its ``_fault_follows`` as it
    starts, before it reads anything.  A raise in its body sets them again.  A
    source that names one of those flags reads it as off.  An instruction with
    ``_fault_settles_idle`` (a Math) also clears them on a scan where it is reached but
    does not run, either because its rung is false or because it is a one-shot that
    does not fire (CLICK, ``flag_steps``, ``flag_reads/oneshot_held``).  Any other
    instruction leaves them as they are on such a scan.
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
                f"Tag '{value.name}' is {value.type.name}, so it cannot be a condition by "
                "itself. Compare it instead, as in tag == value or tag > 0."
            )
        raise TypeError(f"Expected Condition or Tag, got {type(value)}")

    return _normalize_and_condition(
        obj,
        coerce=_coerce,
        empty_error="condition requires at least one condition",
        group_empty_error="condition group cannot be empty",
    )


def instruction_condition_view(ctx: ScanContext, instr: Any) -> ConditionView:
    """Return the condition view that box *instr* reads its helper pins from, taken at box entry.

    A box reads its enable and all its pins at one moment, as it starts and
    before any write of its own, whether or not it is enabled.  The view is
    frozen there.  No pin sees the box's own Done/Acc/step writes, and every pin
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


def resolve_preset_ctx(preset: Preset, ctx: ScanContext) -> int:
    """Return the value of *preset* on this scan."""
    match preset:
        case TagArg(tag=tag):
            return ctx.get_tag(tag.name, tag.default)
        case ConstArg(value=value):
            return value
        case _:
            assert_never(preset)


def status_arg(tag: Tag | None) -> TagArg | None:
    """Return the status bit *tag*, such as a timer's ``TT``, as a ``TagArg``, or None."""
    return None if tag is None else TagArg(tag)


def parse_preset(preset: object, *, label: str) -> Preset:
    """Return *preset* as a tag or a constant, or raise the error that refuses it.

    The preset must be an int or an INT, DINT or WORD tag.  A REAL preset has no single
    comparison every backend agrees on, and CLICK refuses one anyway (DS timer presets,
    DS/DD counter presets).
    """
    from pyrung.core.tag import Tag as TagClass
    from pyrung.core.tag import TagType

    allowed = (TagType.INT, TagType.DINT, TagType.WORD)
    if isinstance(preset, TagClass):
        assert_tag_type(preset, allowed, label=label, include_tag_name=True)
        return TagArg(preset)
    if isinstance(preset, bool) or not isinstance(preset, int):
        raise TypeError(
            f"{label} must be an int or an {_allowed_type_text(allowed)} tag; "
            f"got {type(preset).__name__}"
        )
    return ConstArg(preset)


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
