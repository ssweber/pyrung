"""Small contract for state that must be driven across scan boundaries.

An instruction may own a result that cannot be assigned directly.  A timer's
``Done`` bit, a counter accumulator, and a coupled analog reading are examples:
PILOT must keep another condition true, or make an edge, until the owned value
reaches an observable boundary.

The contract describes only the next such operation.  It does not choose a
route, rank inputs, or describe a complete path.
"""

from __future__ import annotations

import math
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, TypeAlias

from pyrung.core.crossing import AffineCmp, Cmp, Constraint, Eq

if TYPE_CHECKING:
    from pyrung.core.tag import Tag

Snapshot: TypeAlias = Mapping[str, Any]


@dataclass(frozen=True)
class ConditionDemand:
    """A condition that must have *value* while an advance operation runs."""

    condition: Any
    value: bool = True


@dataclass(frozen=True)
class AdvanceStep:
    """The next cross-scan operation for an instruction-owned result."""

    until: Eq | Cmp | AffineCmp
    holds: tuple[ConditionDemand, ...] = ()
    pulse: ConditionDemand | None = None
    # Observable evidence that this exact operation owns continuing motion
    # even when its scalar channel is quantized and does not change this scan.
    progress: ConditionDemand | None = None


@dataclass(frozen=True)
class LinearProgress:
    """Optional scalar support for estimates and progress measurements.

    ``distance`` returns remaining target-relative work. ``estimate_scans``
    receives the PLC scan period explicitly because timer rates depend on it.
    Either callback returns ``None`` when the answer is not analytic.
    """

    direction: int
    distance: Callable[[Constraint, Snapshot], float | None]
    estimate_scans: Callable[[Constraint, Snapshot, float], int | None]


@dataclass(frozen=True)
class AdvanceProfile:
    """How one owner advances its result channels one boundary at a time."""

    channels: tuple[Tag, ...]
    plan: Callable[[Constraint, Snapshot], AdvanceStep | None]
    accumulator: Tag | None = None
    done: Tag | None = None
    linear: LinearProgress | None = None


def resolve_snapshot_value(value: Any, snapshot: Snapshot) -> Any:
    """Resolve a tag-shaped operand or return a literal unchanged."""

    name = getattr(value, "name", None)
    if name is None:
        return value
    return snapshot.get(name, getattr(value, "default", None))


def constraint_holds(constraint: Constraint, snapshot: Snapshot) -> bool | None:
    """Evaluate the scalar constraint shapes used by advance profiles.

    ``None`` means the contract cannot decide the shape.  It never guesses.
    """

    actual = snapshot.get(getattr(constraint, "tag", ""))
    if isinstance(constraint, Eq):
        return actual in constraint.values
    if isinstance(constraint, AffineCmp):
        raw_bound = snapshot.get(constraint.bound_tag)
        if raw_bound is None:
            return None
        try:
            bound = constraint.scale * raw_bound + constraint.offset
        except TypeError:
            return None
    elif isinstance(constraint, Cmp):
        bound = (
            snapshot.get(str(constraint.bound))
            if constraint.bound_is_tag
            else resolve_snapshot_value(constraint.bound, snapshot)
        )
    else:
        return None
    if actual is None or bound is None:
        return None
    try:
        return {
            "==": actual == bound,
            "!=": actual != bound,
            "<": actual < bound,
            "<=": actual <= bound,
            ">": actual > bound,
            ">=": actual >= bound,
        }[constraint.op]
    except (KeyError, TypeError):
        return None


def scalar_boundary(constraint: Constraint, snapshot: Snapshot) -> float | None:
    """Return a numeric boundary for ``Eq``/``Cmp`` when one is available."""

    value: Any
    if isinstance(constraint, Eq):
        if len(constraint.values) != 1:
            return None
        value = next(iter(constraint.values))
    elif isinstance(constraint, Cmp):
        value = (
            snapshot.get(str(constraint.bound))
            if constraint.bound_is_tag
            else resolve_snapshot_value(constraint.bound, snapshot)
        )
    elif isinstance(constraint, AffineCmp):
        raw_value = snapshot.get(constraint.bound_tag)
        if raw_value is None:
            return None
        try:
            value = constraint.scale * raw_value + constraint.offset
        except TypeError:
            return None
    else:
        return None
    if value is None or isinstance(value, bool):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def linear_progress(
    coordinate: Tag,
    *,
    direction: int,
    rate_per_scan: Callable[[float], float],
) -> LinearProgress:
    """Build scalar progress callbacks for a monotone owned coordinate."""

    sign = 1 if direction >= 0 else -1

    def distance(constraint: Constraint, snapshot: Snapshot) -> float | None:
        boundary = scalar_boundary(constraint, snapshot)
        current = snapshot.get(coordinate.name, coordinate.default)
        if boundary is None or isinstance(current, bool):
            return None
        try:
            return max(0.0, sign * (boundary - float(current)))
        except (TypeError, ValueError):
            return None

    def estimate_scans(
        constraint: Constraint,
        snapshot: Snapshot,
        dt: float,
    ) -> int | None:
        remaining = distance(constraint, snapshot)
        if remaining is None:
            return None
        try:
            rate = abs(float(rate_per_scan(dt)))
        except Exception:  # noqa: BLE001 - an unreadable rate means "measure"
            return None
        if rate == 0.0:
            return None
        return int(math.ceil(remaining / rate))

    return LinearProgress(direction=sign, distance=distance, estimate_scans=estimate_scans)


def eq(tag: Tag, value: Any) -> Eq:
    """Convenient exact constraint constructor for a tag."""

    return Eq(tag.name, frozenset((value,)))


def monotone_profile(
    *,
    channels: tuple[Tag, ...],
    accumulator: Tag,
    done: Tag | None,
    progress: ConditionDemand | None,
    preset: Tag | int,
    direction: int,
    rate_per_scan: Callable[[float], float],
    advance: ConditionDemand,
    advance_is_pulse: bool = False,
    done_at_boundary: bool = True,
    restore: ConditionDemand | None = None,
    restore_is_pulse: bool = False,
) -> AdvanceProfile:
    """Build the common timer/counter/analog next-operation contract.

    Instruction classes pass their real conditions into this helper.  The
    returned profile exposes none of those implementation details as fields;
    callers ask ``plan`` for the operation appropriate to the current target.
    """

    sign = 1 if direction >= 0 else -1

    def _preset_value(snapshot: Snapshot) -> int | None:
        raw = resolve_snapshot_value(preset, snapshot)
        try:
            return sign * abs(int(raw))
        except (TypeError, ValueError):
            return None

    def _done_boundary(snapshot: Snapshot) -> Cmp | None:
        preset_name = getattr(preset, "name", None)
        if sign > 0 and preset_name is not None:
            return Cmp(accumulator.name, ">=", preset_name, bound_is_tag=True)
        target = _preset_value(snapshot)
        if target is None:
            return None
        return Cmp(accumulator.name, ">=" if sign > 0 else "<=", target)

    def _operation(
        until: Eq | Cmp | AffineCmp,
        demand: ConditionDemand,
        pulse: bool,
        *,
        progress_receipt: ConditionDemand | None = None,
    ) -> AdvanceStep:
        if pulse:
            return AdvanceStep(until=until, pulse=demand, progress=progress_receipt)
        return AdvanceStep(
            until=until,
            holds=(demand,),
            progress=progress_receipt,
        )

    def plan(constraint: Constraint, snapshot: Snapshot) -> AdvanceStep | None:
        if not isinstance(constraint, (Eq, Cmp, AffineCmp)):
            return None
        held = constraint_holds(constraint, snapshot)
        if held is True:
            return None

        if constraint.tag == accumulator.name:
            boundary = scalar_boundary(constraint, snapshot)
            current = snapshot.get(accumulator.name, accumulator.default)
            if boundary is None:
                return None
            try:
                ahead = sign * (boundary - float(current)) >= 0
            except (TypeError, ValueError):
                return None
            if ahead:
                return _operation(
                    constraint,
                    advance,
                    advance_is_pulse,
                    progress_receipt=progress,
                )
            if restore is not None and boundary == 0:
                return _operation(constraint, restore, restore_is_pulse)
            return None

        if done is None or constraint.tag != done.name or not isinstance(constraint, Eq):
            return None
        if len(constraint.values) != 1:
            return None
        desired = next(iter(constraint.values))
        if not isinstance(desired, bool):
            return None
        if desired is done_at_boundary:
            boundary = _done_boundary(snapshot)
            if boundary is None:
                return None
            # The scalar boundary and its visible Done bit can settle on
            # different scans.  Once Acc is already at the boundary, keep the
            # same operation in place until the requested Done value itself is
            # observable.
            until = constraint if constraint_holds(boundary, snapshot) is True else boundary
            return _operation(
                until,
                advance,
                advance_is_pulse,
                progress_receipt=progress,
            )
        if restore is not None:
            return _operation(eq(done, desired), restore, restore_is_pulse)
        return None

    base_linear = linear_progress(
        accumulator,
        direction=sign,
        rate_per_scan=rate_per_scan,
    )

    def _linear_boundary(
        constraint: Constraint,
        snapshot: Snapshot,
    ) -> Eq | Cmp | AffineCmp | None:
        if not isinstance(constraint, (Eq, Cmp, AffineCmp)):
            return None
        if constraint.tag == accumulator.name:
            return constraint
        step = plan(constraint, snapshot)
        if step is not None and step.until.tag == accumulator.name:
            return step.until
        if constraint_holds(constraint, snapshot) is True:
            return Cmp(
                accumulator.name,
                ">=" if sign > 0 else "<=",
                snapshot.get(accumulator.name, accumulator.default),
            )
        return None

    def distance(constraint: Constraint, snapshot: Snapshot) -> float | None:
        boundary = _linear_boundary(constraint, snapshot)
        return None if boundary is None else base_linear.distance(boundary, snapshot)

    def estimate_scans(
        constraint: Constraint,
        snapshot: Snapshot,
        dt: float,
    ) -> int | None:
        boundary = _linear_boundary(constraint, snapshot)
        return None if boundary is None else base_linear.estimate_scans(boundary, snapshot, dt)

    return AdvanceProfile(
        channels=channels,
        plan=plan,
        accumulator=accumulator,
        done=done,
        linear=LinearProgress(
            direction=sign,
            distance=distance,
            estimate_scans=estimate_scans,
        ),
    )
