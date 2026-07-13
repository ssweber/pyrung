"""The target-relative progress gauge — event-earned work, not channel motion.

``_pilot_state_key`` is a *search* key: it threshold-abstracts recognized
progress sources (essential for finite search), which aliases partial progress
between thresholds — ``(AtDoor, count=1)`` and ``(AtDoor, count=2)`` project to
the same key even though the second knock earned real work.  And raw-key
novelty is cheap to obtain accidentally (868 dims of local actuator and
calendar state).  Neither can say whether a departure *destroyed work*.

This module builds the **gauge**: the small set of retained registers
in the target's causal cone whose writers have *proven discrete provenance* —
work earned by an edge, a command pulse, or a Done/threshold crossing, never a
timer tick.  v1 deliberately recognizes only two provable structural families
and returns nothing (→ ``unknown`` downstream) for everything else:

* **ordinal** — a threshold-absorbed monotone source (the prover masked its raw
  value behind crossing vectors; ``Knock_Count``).  Raw value stays out of the
  search key; here it contributes an *ordinal overlay* in its stride direction.
* **stepper** — a retained sequence register whose writers are constant-stride
  affine steps and/or literal loads (``Internal__Step``): the discrete affine
  steps advance it; a literal load *behind* the current value is a reset
  (``S_Resetting -> 101``), and its enabling channel values are resolved one
  hop through the state-alias Bool so a candidate route can be tested for
  reset residency.

A tag enters the gauge only if **every** effective writer is classifiable;
otherwise it is omitted and any decision that would have needed it must report
``unknown`` rather than guess.  Consumers: the verify SPIN/CYCLE gates (an
ordinal advance is progress even when the search key aliases) and provisional
departure assessment (progress marks at the observed start vs. later worlds).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
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


@dataclass(frozen=True)
class GaugeComponent:
    tag: str
    kind: str  # "ordinal" | "stepper"
    direction: int  # +1 / -1 — the earn direction
    resets: tuple[_ResetWriter, ...] = ()


@dataclass(frozen=True)
class Gauge:
    components: tuple[GaugeComponent, ...]

    @property
    def tags(self) -> tuple[str, ...]:
        return tuple(c.tag for c in self.components)

    def mark(self, snap: Any) -> tuple[tuple[str, Any], ...]:
        """The gauge receipt for one snapshot."""
        return tuple((c.tag, snap.get(c.tag)) for c in self.components)

    def compare(self, anchor: Any, now: Any) -> str:
        """``advanced`` / ``preserved`` / ``behind`` / ``unknown``.

        ``behind`` dominates (any erased component means work was destroyed);
        then ``advanced`` (some component earned); then ``preserved``.
        Non-numeric or missing values are ``unknown`` — never guessed.
        """
        saw_advance = False
        saw_unknown = False
        for c in self.components:
            v0, v1 = anchor.get(c.tag), now.get(c.tag)
            if (
                isinstance(v0, bool)
                or isinstance(v1, bool)
                or not isinstance(v0, (int, float))
                or not isinstance(v1, (int, float))
            ):
                saw_unknown = True
                continue
            delta = (v1 - v0) * c.direction
            if delta < 0:
                return "behind"
            if delta > 0:
                saw_advance = True
        if saw_advance:
            return "advanced"
        return "unknown" if saw_unknown else "preserved"

    def ordinal_advanced(self, before: Any, after: Any) -> bool:
        """Did any component earn in its stride direction between snapshots?

        The verify SPIN/CYCLE consumers use this: a trial that advanced an
        event-earned ordinal did real work even when the threshold-masked
        search key aliases the before/after states.
        """
        for c in self.components:
            v0, v1 = before.get(c.tag), after.get(c.tag)
            if (
                isinstance(v0, bool)
                or isinstance(v1, bool)
                or not isinstance(v0, (int, float))
                or not isinstance(v1, (int, float))
            ):
                continue
            if (v1 - v0) * c.direction > 0:
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


def build_gauge(
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
) -> Gauge:
    """Build the target-relative gauge (see module docstring).

    Conservative on every axis: unknown writer shapes, unresolvable guards, or
    mixed stride directions omit the tag; an empty gauge is a valid answer
    (downstream verdicts then say ``unknown`` instead of guessing).
    """
    from pyrung.core.analysis.pilot.accumulators import iter_profiles

    cone = pdg.upstream_slice(target_tag, follow_calls=True) | {target_tag}

    done_names: set[str] = set()
    profile_acc_names: set[str] = set()
    for profile, _instr in iter_profiles(program, harness=harness):
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

    components: list[GaugeComponent] = []

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
                GaugeComponent(tag=tag, kind="ordinal", direction=direction, resets=verdict)
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
                GaugeComponent(tag=tag, kind="stepper", direction=direction, resets=verdict)
            )

    gauge = Gauge(components=tuple(components))
    if components:
        logger.debug(
            "gauge for %s: %s",
            target_tag,
            ", ".join(f"{c.tag}[{c.kind}{'+' if c.direction > 0 else '-'}]" for c in components),
        )
    return gauge


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
            )
        )
    if not saw_advance:
        return None
    return tuple(resets)
