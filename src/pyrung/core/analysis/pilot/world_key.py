"""Project executable PILOT worlds onto stable search identities."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

_THRESHOLD_DOWN_KINDS = frozenset({"count_down", "int_down", "real_down"})
_THRESHOLD_FORM_GT = "gt"


@dataclass(frozen=True)
class _StateKeyConfig:
    """Projection dimensions for the pilot state key.

    When built from the prover's ``_ExploreContext``, ``stateful_names``
    contains every cross-scan tag, ``done_specs`` carries the Done-bit
    three-valued abstraction, ``threshold_vector_specs`` carries accumulator
    crossing vectors, and ``acc_indices`` marks raw accumulator positions to
    mask.

    When the prover pipeline is unavailable, the fallback uses ``pivot_tags``
    from the trace tree with empty absorption specs.
    """

    stateful_names: tuple[str, ...]
    done_specs: tuple[Any, ...]
    threshold_vector_specs: tuple[Any, ...]
    acc_indices: frozenset[int]


def _threshold_crossed_snap(
    snap: dict[str, Any],
    kind: str,
    acc_name: str,
    threshold: int | float | str,
    form: str,
) -> bool:
    """Threshold-vector bit from a PLC snapshot (mirrors kernel._threshold_crossed)."""
    acc_value = snap.get(acc_name)
    threshold_value = snap.get(threshold) if isinstance(threshold, str) else threshold
    if (
        type(acc_value) is bool
        or type(threshold_value) is bool
        or not isinstance(acc_value, (int, float))
        or not isinstance(threshold_value, (int, float))
    ):
        return False
    if kind in _THRESHOLD_DOWN_KINDS:
        acc_value = -acc_value
        threshold_value = -threshold_value
    if form == _THRESHOLD_FORM_GT:
        return acc_value > threshold_value
    return acc_value >= threshold_value


def _pilot_state_key(snap: dict[str, Any], cfg: _StateKeyConfig) -> tuple[Any, ...]:
    """Project a PLC snapshot onto the state key dimensions."""
    parts: list[Any] = list(map(snap.get, cfg.stateful_names))
    if cfg.done_specs:
        from pyrung.core.analysis.prove.absorb import _done_acc_state

        for spec in cfg.done_specs:
            parts[spec.index] = _done_acc_state(
                spec.kind, parts[spec.index], snap.get(spec.acc_name)
            )
    for idx in cfg.acc_indices:
        parts[idx] = None
    for spec in cfg.threshold_vector_specs:
        parts.append(
            tuple(
                _threshold_crossed_snap(snap, spec.kind, spec.acc_name, atom.threshold, atom.form)
                for atom in spec.atoms
            )
        )
    return tuple(parts)


def wait_edge_nogood(channel_tag: str, from_value: Any, to_value: Any) -> tuple[str, Any]:
    """The world-keyed nogood for a completion (WAIT) edge that proved sterile.

    A completion edge carries no action, so the ordinary ``(tag, value)``
    action nogood can never name it. This synthetic pair — keyed by the channel
    and the exact ``from -> to`` claim — lets a rejected wait be remembered at
    its world key and filtered out of the next iteration's route query, exactly
    like a failed press.
    """
    return (f"wait::{channel_tag}", (from_value, to_value))


def _semantic_key(value: Any) -> Any:
    """A stable, hashable identity for a rung operand or guard.

    ``OperationReceipt`` moved to ``overlay``. Its encoded module token remains
    the old value so already-recorded world identities do not change merely
    because the class gained a truthful module home.
    """
    from enum import Enum

    from pyrung.core.tag import ImmediateRef, Tag

    if value is None or isinstance(value, bool | int | float | str | bytes):
        return value
    if isinstance(value, Tag):
        return ("tag", value.name)
    if isinstance(value, ImmediateRef):
        return ("immediate", _semantic_key(value.value))
    if isinstance(value, Enum):
        return (type(value).__module__, type(value).__qualname__, value.name)
    if isinstance(value, tuple | list):
        return tuple(_semantic_key(item) for item in value)
    if isinstance(value, set | frozenset):
        return tuple(sorted((_semantic_key(item) for item in value), key=repr))
    if isinstance(value, dict):
        return tuple(
            sorted(
                ((_semantic_key(k), _semantic_key(v)) for k, v in value.items()),
                key=repr,
            )
        )
    attrs = getattr(value, "__dict__", None)
    if attrs is not None:
        semantic = tuple(
            (name, _semantic_key(member))
            for name, member in sorted(attrs.items())
            if not name.startswith("_") and name not in {"source_file", "source_line"}
        )
        module = type(value).__module__
        qualname = type(value).__qualname__
        if module == "pyrung.core.analysis.pilot.overlay" and qualname == "OperationReceipt":
            module = "pyrung.core.analysis.pilot._ops"
        return (module, qualname, semantic)
    return (type(value).__module__, type(value).__qualname__, str(value))


def _rung_identity(rung: Any) -> tuple[Any, ...]:
    """Exact executable identity used for overlay ownership and deduplication.

    Correction nogoods are identities of replay-confirmed executable rungs
    composed here; guard and operation boundary are part of the disproved
    artifact. A raw ``(tag, value)`` hypothesis does not own a scope and cannot
    be compared with revoked evidence until investigation materializes its
    guarded installed form.
    """
    return (
        rung.dest,
        _semantic_key(rung.value),
        _semantic_key(rung.guard),
        _semantic_key(rung.operation),
    )


def _pilot_world_key(
    snap: dict[str, Any],
    cfg: _StateKeyConfig,
    rungs: Any,
) -> tuple[Any, ...]:
    """Identity of an executable PILOT world: PLC projection plus PilotRungs."""
    rung_key = tuple(_rung_identity(rung) for rung in rungs)
    return (_pilot_state_key(snap, cfg), rung_key)
