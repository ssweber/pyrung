"""Read program-awaited actions from the current machine state.

``awaited_actions`` recognizes channel transitions that are presently waiting
on operator actions. The module also classifies sibling
producer families so an automatic producer is not erased when an equivalent
operator action is disallowed.

These capabilities consume ``WalkContext`` and program structure only. They
return structural evidence without applying avoid, ambiguity, or precedence
policy, and they never execute the program or retain a route.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from pyrung.core.analysis.pdg import TagRole, resolve_rung
from pyrung.core.analysis.pilot.types import WalkContext
from pyrung.core.analysis.simplified import _sp_to_expr
from pyrung.core.analysis.sp_values import (
    _extract_condition_values,
    _values_match,
    _written_value_for_tag,
)
from pyrung.core.crossing import UNKNOWN, Affine, Literal, evaluate_forward


@dataclass(frozen=True)
class AwaitedAction:
    """One operator action a program transition is waiting for.

    A bearing, not a plan step: ``action`` is the ``(tag, level)`` push, and the
    remaining fields are the recognized ``(state, command, next-state)`` context
    recorded for legibility (every awaited-action decision is dumpable).
    """

    action: tuple[str, Any]
    command_tag: str
    command_value: Any
    # Every command-gate write the push supplies. A request strobe alone is
    # shared by many unrelated commands, so consumers must compare the whole
    # signature before deciding an automatic producer subsumes this action.
    command_writes: tuple[tuple[str, Any], ...]
    from_state: Any
    to_state: Any
    note: str


def _rung_condition(ctx: WalkContext, rung_idx: int) -> Any:
    rn = ctx.pdg.rung_nodes[rung_idx]
    ro = resolve_rung(ctx.program, rn)
    if ro is None:
        return None, None
    sp = ro.sp_tree()
    cond = _sp_to_expr(sp) if sp is not None else None
    return ro, cond


@dataclass(frozen=True)
class _Transition:
    """A command-gated transition off the current state."""

    to_value: Any
    command_guards: dict[str, frozenset[Any]]  # non-channel constraints (Cmd==5, CmdReq==1)


def _state_transitions(
    ctx: WalkContext,
    channel_tag: str,
    state_value: Any,
    request_tags: frozenset[str],
) -> list[_Transition]:
    """Command-gated transitions that fire *from the current state*.

    A transition is a rung writing the channel register or one of its request
    tags, gated on ``channel == state_value`` and on one or more non-steerable
    command registers ``C == cv``.  Its ``command_guards`` are those command
    constraints (``Cmd == 5, CmdReq == 1``) — the discriminating values a push
    must produce for the transition to fire.
    """
    transitions: list[_Transition] = []
    for target in {channel_tag} | set(request_tags):
        for ri in ctx.pdg.writers_of.get(target, frozenset()):
            ro, cond = _rung_condition(ctx, ri)
            if cond is None:
                continue
            cvals = _extract_condition_values(cond)
            gate = cvals.get(channel_tag)
            if gate is None or not any(_values_match(v, state_value) for v in gate):
                continue
            guards = {
                ctag: cvs
                for ctag, cvs in cvals.items()
                if ctag != channel_tag and ctag not in ctx.steerable
            }
            if not guards:
                continue
            written = _written_value_for_tag(ro, target)
            to_value = written.value if isinstance(written, Literal) else None
            transitions.append(_Transition(to_value=to_value, command_guards=guards))
    return transitions


def _button_writes(ctx: WalkContext, button: str, snapshot: dict[str, Any]) -> dict[str, Any]:
    """Command-register literals the producers gated by *button* set when it is
    pressed now.

    A producer counts only when its whole guard is satisfied once the button is
    pressed — so the push is the sole missing term (any state guard already
    holds).  Program-owned producers (``rise(Tmr.Done)`` — no steerable button)
    are not gated by a button, so they never contribute: the program issues those
    itself; they are coasts, not pushes.
    """
    from pyrung.core.analysis.prove.expr import _eval_expr_from_state

    writes: dict[str, Any] = {}
    overlay = {**snapshot, button: True}
    for ri, rn in enumerate(ctx.pdg.rung_nodes):
        if button not in rn.condition_reads:
            continue
        ro, cond = _rung_condition(ctx, ri)
        if cond is None:
            continue
        if _eval_expr_from_state(cond, overlay) is not True:
            continue
        for tag in rn.writes:
            if tag in ctx.steerable:
                continue
            written = _written_value_for_tag(ro, tag)
            if isinstance(written, Literal):
                writes[tag] = written.value
    return writes


def _transition_fires(
    transition: _Transition, writes: dict[str, Any], snapshot: dict[str, Any]
) -> bool:
    """Whether *transition*'s command guards are met by a push's *writes* (falling
    back to the live snapshot for terms the push does not touch)."""
    for tag, vals in transition.command_guards.items():
        if tag in writes:
            if not any(_values_match(writes[tag], v) for v in vals):
                return False
        elif not any(_values_match(snapshot.get(tag), v) for v in vals):
            return False
    return True


def _request_tags_for(channel_tag: str, pipeline_roles: Any) -> frozenset[str]:
    tags: set[str] = set()
    for role in pipeline_roles or ():
        if role.channel_tag == channel_tag:
            tags |= set(role.request_tags)
    return frozenset(tags)


def awaited_actions(
    ctx: WalkContext,
    channel_tag: str,
    pipeline_roles: Any,
) -> tuple[AwaitedAction, ...]:
    """All structural operator-action readings for the current channel state."""
    snapshot = dict(ctx.snapshot)
    state_value = snapshot.get(channel_tag)
    if state_value is None:
        return ()

    request_tags = _request_tags_for(channel_tag, pipeline_roles)
    transitions = _state_transitions(ctx, channel_tag, state_value, request_tags)
    if not transitions:
        return ()

    candidates: list[AwaitedAction] = []
    seen_buttons: set[str] = set()
    for button in sorted(ctx.steerable):
        if button in seen_buttons:
            continue
        action = (button, True)
        writes = _button_writes(ctx, button, snapshot)
        if not writes:
            continue
        for transition in transitions:
            if not _transition_fires(transition, writes, snapshot):
                continue
            # A push that leaves the state unmoved (a re-request of the current
            # value) is not a drive.
            if transition.to_value is not None and _values_match(transition.to_value, state_value):
                continue
            command_desc = ", ".join(f"{t}={writes[t]!r}" for t in sorted(writes) if t in writes)
            seen_buttons.add(button)
            command_writes = tuple(
                sorted((tag, writes[tag]) for tag in transition.command_guards if tag in writes)
            )
            candidates.append(
                AwaitedAction(
                    action=action,
                    command_tag=next(iter(transition.command_guards), ""),
                    command_value=writes.get(next(iter(transition.command_guards), "")),
                    command_writes=command_writes,
                    from_state=state_value,
                    to_state=transition.to_value,
                    note=(
                        f"program-awaited action: {channel_tag}={state_value!r} awaits "
                        f"{button} ({command_desc}) -> {channel_tag}={transition.to_value!r}"
                    ),
                )
            )
            break

    return tuple(candidates)


# ---------------------------------------------------------------------------
# Const-fold of program-constant copy sources (capability piece 1)
# ---------------------------------------------------------------------------
#
# The command pipeline's program-owned producers write the command register from
# an *init-constant* copy source: ``copy(CmdCompleteRef, C_CtrlCmd)`` where
# ``CmdCompleteRef`` is ``ds.slot(370, default=10)`` — a never-program-written
# constant, the same declaration shape ``compute_reference_constants`` blesses for
# the ``sm__STATE*REF`` tables.  Value-by-literal producer search sees only the
# avoided operator button (the sole *literal* writer of 10); the program-owned
# producer is invisible until the const source is folded to its default.
#
# This is a **static const-fold**, not a runtime resolution: the source is
# provably a program constant, so its value is fixed at its declared default.


def is_program_constant(name: str, ctx: WalkContext) -> bool:
    """Whether *name* is a program constant (a never-written, non-lever register).

    Reuses the exact distinction ``compute_reference_constants`` draws (a
    reference constant has **no program writers** and is **not** an operator/field
    interface).  In a ``WalkContext`` the drive-layer ``steerable`` set has already
    had the reference constants subtracted, so an operator-facing word is *in*
    ``steerable`` while a program constant is not — the WalkContext-only way to
    read "not a lever" without the ``known`` tag dict.  Fail-closed: any writer, or
    membership in ``steerable``, means not a constant.
    """
    if not name:
        return False
    if ctx.pdg.writers_of.get(name, frozenset()):
        return False
    if name in ctx.steerable:
        return False
    return True


def fold_const_copy_source(wv: Any, ctx: WalkContext) -> Literal | None:
    """Fold an identity/scaled copy from a program-constant source to a literal.

    ``copy(src, dest)`` / ``calc(src * k + b, dest)`` where *src* is a program
    constant (:func:`is_program_constant`) produces the fixed value
    ``src.default * scale + offset``.  Returns that as a :class:`Literal`, so a
    producer search matching "this writer produces value v" sees the program-owned
    producer.  Returns ``None`` (punt, never fabricate) when *wv* is not an affine
    copy, the source is not a program constant (has any program writer, or is a
    steerable/external lever), or the source value is not statically resolvable.
    """
    if not isinstance(wv, Affine):
        return None
    if not is_program_constant(wv.source, ctx):
        return None
    src = ctx.snapshot.get(wv.source)
    if src is None:
        tag_obj = ctx.pdg.tags.get(wv.source)
        src = getattr(tag_obj, "default", None)
    if not isinstance(src, (int, float, bool)):
        return None
    produced = evaluate_forward(wv, {wv.source: src})
    return None if produced is UNKNOWN else Literal(produced)


def producer_value(ctx: WalkContext, ro: Any, tag: str) -> Any:
    """The concrete value *ro* drives into *tag*, const-folding a constant source.

    A literal write gives its value; an identity/scaled copy from a program
    constant gives the folded default (:func:`fold_const_copy_source`).  ``None``
    when the value is not statically knowable (a live-state copy, an aggregate).
    """
    wv = _written_value_for_tag(ro, tag)
    if isinstance(wv, Literal):
        return wv.value
    folded = fold_const_copy_source(wv, ctx)
    return folded.value if folded is not None else None


# ---------------------------------------------------------------------------
# Sibling producer families (capability piece 2)
# ---------------------------------------------------------------------------
#
# The writers of a pipeline command register (``C_CtrlCmd``) that co-write the
# request strobe (``C_CmdChgRequestBool := 1``) and produce one command value form
# a *family*: the operator button, the program-owned rung (``rise(Tmr.Done)``), and
# the environmental rung (door-open) that all issue the same command.  Their guards
# are the pipeline's input alphabet.  A steerable exemplar is SUFFICIENT evidence of
# pipeline-ness but NOT necessary — the S_StateCompleteBool completion pipeline has
# three program-owned producers and zero operator buttons.


@dataclass(frozen=True)
class Producer:
    """One writer in a command-value producer family."""

    rung_index: int
    kind: str  # "operator" | "program" | "environmental" | "ambiguous"
    guard_tags: frozenset[str]
    co_writes: frozenset[str]  # other command/request registers this rung writes
    command_tag: str
    command_value: Any


@dataclass(frozen=True)
class ProducerFamily:
    """The writers that issue one command value into a pipeline register."""

    command_tag: str
    value: Any
    producers: tuple[Producer, ...]

    @property
    def has_steerable_exemplar(self) -> bool:
        return any(p.kind == "operator" for p in self.producers)

    @property
    def program_owned(self) -> tuple[Producer, ...]:
        return tuple(p for p in self.producers if p.kind == "program")


def _classify_producer_guard(ctx: WalkContext, rung_idx: int) -> tuple[str, frozenset[str]]:
    """Classify a producer rung's guard as operator / program / environmental.

    * a **steerable** guard tag (an operator button) ⇒ ``"operator"``;
    * a **physical input** guard tag (``TagRole.INPUT``) ⇒ ``"environmental"``;
    * otherwise (timer done / step / state registers only) ⇒ ``"program"``.

    Fail-closed only where nothing is readable: an empty guard is ``"ambiguous"``.
    """
    rn = ctx.pdg.rung_nodes[rung_idx]
    reads = frozenset(rn.condition_reads)
    if not reads:
        return "ambiguous", reads
    if reads & ctx.steerable:
        return "operator", reads
    if any(ctx.pdg.tag_roles.get(t) == TagRole.INPUT for t in reads):
        return "environmental", reads
    return "program", reads


def sibling_producer_family(
    ctx: WalkContext, command_tag: str, value: Any
) -> ProducerFamily | None:
    """The family of writers that issue ``command_tag == value``.

    Groups every writer of *command_tag* that produces *value* — matching literal
    writes AND const-folded copies from program constants (:func:`producer_value`)
    — and classifies each guard.  This is what makes the program-owned producer
    (``copy(CmdCompleteRef, C_CtrlCmd)`` guarded ``rise(S_Shining_tmr.Done)``)
    visible beside the avoided operator button (``copy(10, C_CtrlCmd)`` guarded
    ``C_Complete``): both produce 10, so both join the family.

    Returns ``None`` when no writer produces the value.  A family with no operator
    exemplar is still valid (the completion pipeline shape).
    """
    producers: list[Producer] = []
    for ri in sorted(ctx.pdg.writers_of.get(command_tag, frozenset())):
        rn = ctx.pdg.rung_nodes[ri]
        ro = resolve_rung(ctx.program, rn)
        if ro is None:
            continue
        if not _values_match(producer_value(ctx, ro, command_tag), value):
            continue
        kind, guard_tags = _classify_producer_guard(ctx, ri)
        co_writes = frozenset(
            w
            for w in rn.writes
            if w != command_tag and (w in ctx.opaque_loop or w in ctx.steerable)
        )
        producers.append(
            Producer(
                rung_index=ri,
                kind=kind,
                guard_tags=guard_tags,
                co_writes=co_writes,
                command_tag=command_tag,
                command_value=value,
            )
        )
    if not producers:
        return None
    return ProducerFamily(command_tag=command_tag, value=value, producers=tuple(producers))
