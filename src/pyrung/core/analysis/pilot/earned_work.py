"""Measure conservative, target-relative work that search keys may alias.

``build_earned_work`` recognizes retained ordinal and stepper registers whose writers
all have classifiable discrete provenance. An ``EarnedWork`` compares marks across
worlds and exposes reset boundaries used by verification and departure
classification.

If any effective writer of a component is unclassifiable, that component is
omitted. Consumers receive unknown rather than inferred progress from an
incomplete earned-work model.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING, Any

from pyrung.core.analysis.pdg import resolve_rung
from pyrung.core.condition import (
    AllCondition,
    AnyCondition,
    CompareEq,
    FallingEdgeCondition,
    RisingEdgeCondition,
)

if TYPE_CHECKING:
    from pyrung.core.analysis.pdg import ProgramGraph

logger = logging.getLogger(__name__)

_RELAY_DEPTH = 3


# ---------------------------------------------------------------------------
# Cut structure
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _ResetWriter:
    """A literal load that can move a stepper back: value + where it's enabled.

    ``enabling_channel_values`` are the channel values under which the writer's
    guard can hold, resolved one hop through state-alias Bools (empty when the
    guard could not be resolved — such a reset poisons route verdicts to
    ``unknown`` rather than being ignored).
    """

    value: Any
    channel_tag: str | None
    enabling_channel_values: tuple[Any, ...]
    resolved: bool
    init_only: bool  # unconditional first-scan init — not reachable on a route
    writer_rung: int


@dataclass(frozen=True)
class EarnedWorkComponent:
    tag: str
    kind: str  # "ordinal" | "stepper"
    direction: int  # +1 / -1 — the earn direction
    resets: tuple[_ResetWriter, ...] = ()


class EarnedWorkMovement(StrEnum):
    """Observed target-relative motion, without a policy judgment."""

    FORWARD = "forward"
    BACKWARD = "backward"
    UNCHANGED = "unchanged"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class EarnedWorkReading:
    """One earned-work component observed across one world transition."""

    tag: str
    source: Any
    landing: Any
    direction: int

    @property
    def movement(self) -> EarnedWorkMovement:
        if (
            isinstance(self.source, bool)
            or isinstance(self.landing, bool)
            or not isinstance(self.source, (int, float))
            or not isinstance(self.landing, (int, float))
        ):
            return EarnedWorkMovement.UNKNOWN
        delta = (self.landing - self.source) * self.direction
        if delta < 0:
            return EarnedWorkMovement.BACKWARD
        if delta > 0:
            return EarnedWorkMovement.FORWARD
        return EarnedWorkMovement.UNCHANGED


@dataclass(frozen=True)
class EarnedWorkReceipt:
    """Auditable target-relative movement across one world transition."""

    readings: tuple[EarnedWorkReading, ...] = ()

    @property
    def source_mark(self) -> tuple[tuple[str, Any], ...]:
        return tuple((reading.tag, reading.source) for reading in self.readings)

    @property
    def landing_mark(self) -> tuple[tuple[str, Any], ...]:
        return tuple((reading.tag, reading.landing) for reading in self.readings)

    @property
    def movement(self) -> EarnedWorkMovement:
        movements = tuple(reading.movement for reading in self.readings)
        if EarnedWorkMovement.BACKWARD in movements:
            return EarnedWorkMovement.BACKWARD
        if EarnedWorkMovement.FORWARD in movements:
            return EarnedWorkMovement.FORWARD
        if not movements or EarnedWorkMovement.UNKNOWN in movements:
            return EarnedWorkMovement.UNKNOWN
        return EarnedWorkMovement.UNCHANGED

    @property
    def any_forward(self) -> bool:
        return any(reading.movement is EarnedWorkMovement.FORWARD for reading in self.readings)


def legacy_earned_work_movement(movement: EarnedWorkMovement) -> str:
    """Project plain movement names onto stable event-payload vocabulary."""
    return {
        EarnedWorkMovement.FORWARD: "advanced",
        EarnedWorkMovement.BACKWARD: "behind",
        EarnedWorkMovement.UNCHANGED: "preserved",
        EarnedWorkMovement.UNKNOWN: "unknown",
    }[movement]


@dataclass(frozen=True)
class EarnedWork:
    components: tuple[EarnedWorkComponent, ...]

    @property
    def tags(self) -> tuple[str, ...]:
        return tuple(c.tag for c in self.components)

    def mark(self, snap: Any) -> tuple[tuple[str, Any], ...]:
        """The earned-work mark for one snapshot."""
        return tuple((c.tag, snap.get(c.tag)) for c in self.components)

    def receipt(self, source: Any, landing: Any) -> EarnedWorkReceipt:
        """Freeze the target-relative work comparison for one transition."""
        return EarnedWorkReceipt(
            tuple(
                EarnedWorkReading(
                    tag=component.tag,
                    source=source.get(component.tag),
                    landing=landing.get(component.tag),
                    direction=component.direction,
                )
                for component in self.components
            )
        )

    def has_banked_work(self, snap: Any) -> bool:
        """Whether a component is ahead of one of its proved reset floors.

        This is a current-world fact, not a comparison with PILOT history.  It
        lets a fresh read recognize recipe work that the user program completed
        before PILOT arrived: Step 103 is live work beyond the proved Step 101
        reset, while a cold-boot Step 101 is not.
        """

        for component in self.components:
            current = snap.get(component.tag)
            if isinstance(current, bool) or not isinstance(current, (int, float)):
                continue
            for reset in component.resets:
                floor = reset.value
                if (
                    not reset.resolved
                    or isinstance(floor, bool)
                    or not isinstance(floor, (int, float))
                ):
                    continue
                if (current - floor) * component.direction > 0:
                    return True
        return False


# ---------------------------------------------------------------------------
# Discrete provenance — "every satisfiable guard arm contains an event"
# ---------------------------------------------------------------------------


def _discrete_condition(
    cond: Any,
    done_names: frozenset[str],
    clear_only: frozenset[str],
    edge_tags: frozenset[str],
    pdg: ProgramGraph,
    program: Any,
    depth: int,
) -> bool:
    """Whether *cond* fires only on a discrete event.

    ``And`` needs one discrete conjunct; ``Or`` needs every arm discrete (one
    edge somewhere inside an Or must not bless a level-driven writer).  Atoms:
    explicit rise/fall; a read of a Done bit or an ack-cleared (clear-only)
    command; or a pulse-relay Bool whose own writers are all discrete.
    """
    if isinstance(cond, (RisingEdgeCondition, FallingEdgeCondition)):
        return True
    if isinstance(cond, AllCondition):
        return any(
            _discrete_condition(c, done_names, clear_only, edge_tags, pdg, program, depth)
            for c in getattr(cond, "conditions", ())
        )
    if isinstance(cond, AnyCondition):
        arms = getattr(cond, "conditions", ())
        return bool(arms) and all(
            _discrete_condition(c, done_names, clear_only, edge_tags, pdg, program, depth)
            for c in arms
        )
    read = getattr(getattr(cond, "tag", None), "name", None)
    if read is None:
        return False
    if read in done_names or read in clear_only or read in edge_tags:
        return True
    return _discrete_relay(read, done_names, clear_only, edge_tags, pdg, program, depth)


def _rung_conditions(ro: Any) -> tuple[Any, ...]:
    return tuple(getattr(ro, "_conditions", ()) or ())


def _writer_discrete(
    ro: Any,
    done_names: frozenset[str],
    clear_only: frozenset[str],
    edge_tags: frozenset[str],
    pdg: ProgramGraph,
    program: Any,
    depth: int = 0,
) -> bool:
    """The rung's guard (top-level conjunction) contains a discrete conjunct."""
    conds = _rung_conditions(ro)
    if not conds:
        return False  # unconditional — level/init, not event-earned
    return any(
        _discrete_condition(c, done_names, clear_only, edge_tags, pdg, program, depth)
        for c in conds
    )


def _discrete_relay(
    tag: str,
    done_names: frozenset[str],
    clear_only: frozenset[str],
    edge_tags: frozenset[str],
    pdg: ProgramGraph,
    program: Any,
    depth: int,
) -> bool:
    """A pulse relay: a Bool whose every writer is itself discrete-proven."""
    if depth >= _RELAY_DEPTH:
        return False
    writer_idxs = pdg.writers_of.get(tag, frozenset())
    if not writer_idxs:
        return False
    for ri in writer_idxs:
        ro = resolve_rung(program, pdg.rung_nodes[ri])
        if ro is None or not _writer_discrete(
            ro, done_names, clear_only, edge_tags, pdg, program, depth + 1
        ):
            return False
    return True


def _tag_coupled(read: str, tag: str, pdg: ProgramGraph, depth: int = 0) -> bool:
    """Whether *read* is a derivation of *tag* (bounded transitive, writers-side).

    Every writer of *read* must read *tag* — directly, or through another
    tag-coupled register (``valstepisodd`` at one hop; a ``TransBool`` armed
    under step-derived ``S_CurrStep_*`` flags at two).
    """
    if depth >= _RELAY_DEPTH:
        return False
    writer_idxs = pdg.writers_of.get(read, frozenset())
    if not writer_idxs:
        return False
    for ri in writer_idxs:
        node = pdg.rung_nodes[ri]
        reads = node.condition_reads | node.data_reads
        if tag in reads:
            continue
        if not any(_tag_coupled(r, tag, pdg, depth + 1) for r in sorted(reads)):
            return False
    return True


def _self_limiting_advance(rn: Any, ro: Any, tag: str, pdg: ProgramGraph) -> bool:
    """A stepper advance whose guard reads a derivation of the tag itself.

    The write invalidates its own guard context (the parity scratch, the
    step-flag-armed transition pulse), so the writer fires once per context
    entry — a structural discreteness proof: the event is "context entered".
    A free-running ``count + 1`` gated by a clock or a sensor level has no
    tag-coupled conjunct and stays rejected.
    """
    del ro
    return any(_tag_coupled(read, tag, pdg) for read in sorted(rn.condition_reads))


# ---------------------------------------------------------------------------
# Writer shape extraction
# ---------------------------------------------------------------------------


def _literal_or_affine_write(ro: Any, tag: str) -> tuple[str, Any] | None:
    """``("literal", v)`` / ``("affine", offset)`` for self-stride, else None."""
    from pyrung.core.analysis.sp_values import Affine, Literal, _written_value_for_tag

    wv = _written_value_for_tag(ro, tag)
    if isinstance(wv, Literal):
        return ("literal", wv.value)
    if isinstance(wv, Affine) and wv.source == tag and wv.scale == 1 and wv.offset != 0:
        return ("affine", wv.offset)
    return None


def _enabling_channel_values(
    rn: Any,
    ro: Any,
    channel_tags: frozenset[str],
    pdg: ProgramGraph,
    program: Any,
) -> tuple[str | None, tuple[Any, ...], bool]:
    """Resolve which channel values enable this writer, one alias hop deep.

    Direct: the guard compares a channel tag to a literal.  One hop: the guard
    reads a Bool whose own writers are channel-equality-gated (the
    ``sm_MapVal2State`` alias idiom).  Returns ``(channel_tag, values,
    resolved)`` — unresolved resets must poison verdicts, not vanish.
    """

    def _eq_values(conds: tuple[Any, ...]) -> tuple[str | None, tuple[Any, ...]]:
        for cond in conds:
            if isinstance(cond, CompareEq):
                name = getattr(getattr(cond, "tag", None), "name", None)
                if name in channel_tags:
                    other = getattr(cond, "value", None)
                    literal = getattr(other, "name", None)
                    if (
                        literal is None
                        and isinstance(other, (int, float))
                        and not isinstance(other, bool)
                    ):
                        return name, (other,)
        return None, ()

    chan, values = _eq_values(_rung_conditions(ro))
    if chan is not None:
        return chan, values, True

    # One alias hop: a guard Bool written under a channel equality.
    for read in sorted(rn.condition_reads):
        if read in channel_tags:
            continue
        writer_idxs = pdg.writers_of.get(read, frozenset())
        if not writer_idxs or len(writer_idxs) > 4:
            continue
        alias_values: list[Any] = []
        alias_chan: str | None = None
        ok = True
        for ri in writer_idxs:
            aro = resolve_rung(program, pdg.rung_nodes[ri])
            if aro is None:
                ok = False
                break
            chan_i, vals_i = _eq_values(_rung_conditions(aro))
            if chan_i is None:
                ok = False
                break
            alias_chan = alias_chan or chan_i
            alias_values.extend(vals_i)
        if ok and alias_chan is not None and alias_values:
            return alias_chan, tuple(alias_values), True
    return None, (), False


def _is_init_only(rn: Any, ro: Any) -> bool:
    """Unconditional writer (no guard) — first-scan init, unreachable on a route."""
    return not _rung_conditions(ro) and not rn.condition_reads


# ---------------------------------------------------------------------------
# The builder
# ---------------------------------------------------------------------------


def build_earned_work(
    pdg: ProgramGraph,
    program: Any,
    target_tag: str,
    key_config: Any,
    *,
    steerable: frozenset[str],
    clear_only: frozenset[str],
    edge_tags: frozenset[str],
    pipeline_internal_tags: frozenset[str],
    channel_tags: frozenset[str],
    harness: Any = None,
) -> EarnedWork:
    """Build the target-relative earned-work model (see module docstring).

    Conservative on every axis: unknown writer shapes, unresolvable guards, or
    mixed stride directions omit the tag; an empty model is a valid answer
    (downstream verdicts then say ``unknown`` instead of guessing).
    """
    from pyrung.core.analysis.pilot.advance import iter_advance_owners

    cone = pdg.upstream_slice(target_tag, follow_calls=True) | {target_tag}

    done_names: set[str] = set()
    profile_acc_names: set[str] = set()
    for owner in iter_advance_owners(program, harness=harness):
        profile = owner.profile
        acc = getattr(profile, "accumulator", None)
        if acc is not None and getattr(acc, "name", None):
            profile_acc_names.add(acc.name)
        done = getattr(profile, "done", None)
        if done is not None and getattr(done, "name", None):
            done_names.add(done.name)
    if key_config is not None:
        for spec in getattr(key_config, "done_specs", ()):
            name = getattr(spec, "acc_name", None)
            if name:
                profile_acc_names.add(name)

    done_frozen = frozenset(done_names)
    stateful = frozenset(getattr(key_config, "stateful_names", ()) or ())

    excluded = (
        steerable
        | clear_only
        | pipeline_internal_tags
        | channel_tags
        | frozenset(profile_acc_names)
    )

    components: list[EarnedWorkComponent] = []

    # ── Family A: threshold-absorbed monotone sources (ordinal overlay) ──
    ordinal_candidates: dict[str, int] = {}
    for spec in getattr(key_config, "threshold_vector_specs", ()) or ():
        acc = getattr(spec, "acc_name", None)
        kind = str(getattr(spec, "kind", ""))
        if not acc or acc in profile_acc_names or acc in excluded or acc not in cone:
            continue
        ordinal_candidates[acc] = -1 if "down" in kind else 1

    for tag, direction in sorted(ordinal_candidates.items()):
        verdict = _classify_stride_tag(
            tag, direction, pdg, program, done_frozen, clear_only, edge_tags, channel_tags
        )
        if verdict is not None:
            components.append(
                EarnedWorkComponent(tag=tag, kind="ordinal", direction=direction, resets=verdict)
            )

    # ── Family B: discrete stepper registers ──
    ordinal_tags = {c.tag for c in components}
    for tag in sorted(cone & stateful):
        if tag in excluded or tag in ordinal_tags or tag == target_tag:
            continue
        shapes = _stepper_shapes(tag, pdg, program)
        if shapes is None:
            continue
        direction = shapes["direction"]
        verdict = _classify_stride_tag(
            tag, direction, pdg, program, done_frozen, clear_only, edge_tags, channel_tags
        )
        if verdict is not None:
            components.append(
                EarnedWorkComponent(tag=tag, kind="stepper", direction=direction, resets=verdict)
            )

    earned_work = EarnedWork(components=tuple(components))
    if components:
        logger.debug(
            "earned work for %s: %s",
            target_tag,
            ", ".join(f"{c.tag}[{c.kind}{'+' if c.direction > 0 else '-'}]" for c in components),
        )
    return earned_work


def _stepper_shapes(tag: str, pdg: ProgramGraph, program: Any) -> dict[str, Any] | None:
    """Pre-screen for family B: all writers literal or +stride affine, one direction."""
    writer_idxs = pdg.writers_of.get(tag, frozenset())
    if not writer_idxs:
        return None
    offsets: list[Any] = []
    saw_affine = False
    for ri in writer_idxs:
        ro = resolve_rung(program, pdg.rung_nodes[ri])
        if ro is None:
            return None
        shape = _literal_or_affine_write(ro, tag)
        if shape is None:
            return None
        kind, val = shape
        if kind == "affine":
            saw_affine = True
            offsets.append(val)
    if not saw_affine:
        return None  # literal-only registers have no provable stride axis
    direction = 1 if offsets[0] > 0 else -1
    if any((off > 0) != (direction > 0) for off in offsets):
        return None  # conflicting strides — ambiguous, omit
    return {"direction": direction}


def _classify_stride_tag(
    tag: str,
    direction: int,
    pdg: ProgramGraph,
    program: Any,
    done_names: frozenset[str],
    clear_only: frozenset[str],
    edge_tags: frozenset[str],
    channel_tags: frozenset[str],
) -> tuple[_ResetWriter, ...] | None:
    """Classify every writer of an ordered tag; None when any is unclassifiable.

    ADVANCE = stride in *direction* with discrete provenance.  ERASE = a
    literal load (its direction is anchor-relative, judged at use time) or a
    stride against direction.  Anything else — or an advance without discrete
    provenance — disqualifies the tag.
    """
    writer_idxs = pdg.writers_of.get(tag, frozenset())
    if not writer_idxs:
        return None
    resets: list[_ResetWriter] = []
    saw_advance = False
    for ri in sorted(writer_idxs):
        rn = pdg.rung_nodes[ri]
        ro = resolve_rung(program, rn)
        if ro is None:
            return None
        shape = _literal_or_affine_write(ro, tag)
        if shape is None:
            return None
        kind, val = shape
        if kind == "affine" and (val > 0) == (direction > 0):
            if not _writer_discrete(
                ro, done_names, clear_only, edge_tags, pdg, program
            ) and not _self_limiting_advance(rn, ro, tag, pdg):
                return None  # level-driven advance — not event-earned
            saw_advance = True
            continue
        # A literal load (or counter-directional stride): a reset candidate.
        init_only = _is_init_only(rn, ro)
        chan, values, resolved = (
            (None, (), True)
            if init_only
            else _enabling_channel_values(rn, ro, channel_tags, pdg, program)
        )
        resets.append(
            _ResetWriter(
                value=val if kind == "literal" else None,
                channel_tag=chan,
                enabling_channel_values=values,
                resolved=resolved,
                init_only=init_only,
                writer_rung=ri,
            )
        )
    if not saw_advance:
        return None
    return tuple(resets)
