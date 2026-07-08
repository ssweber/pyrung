"""currents.py — recognize when the *program itself* is a current running toward
the target, and surface the one operator action it is waiting for to ride it.

A program-owned current is the program's own self-driving motion — you don't
fight it, you read its set and drift and ride it with the one push it needs.

A **read-side capability** (the walk-context seam — see ``pilot/CLAUDE.md``,
"Where new read-side capabilities live"): it reads the charts, never runs the
ship.  It consumes only a :class:`~pyrung.core.analysis.pilot.types.WalkContext`
(``snapshot`` / ``pdg`` / ``program`` / ``steerable`` / ``opaque_loop`` /
``prior``) plus the channel state register, and returns a bearing — never a
stored plan.

The problem it closes (future-direction item 0, the "drive capability").  A
PackML-shaped state machine reaches its terminal command through a deliberate
lateral detour *out of the acceptance region*: EXECUTE issues an internal Hold
(EXECUTE→HELD), the operator supplies one ack, the program issues Unhold
(HELD→EXECUTE) and then, one recipe step later, self-issues the terminal command
(EXECUTE→COMPLETING→COMPLETED).  At both program-owned transitions **no command
tag is the answer** — the recognizable signal is the ``(step register, state)``
pair, and the whole detour contains exactly the operator actions that are legal
only in a tight ``(state, step)`` window.

At a stall the pilot's backward trace dead-ends on the opaque-loop state register
(the feedback guard punts to the compass), and the compass value-graph route is
the avoided operator command — so nothing surfaces the *one* legal operator
action (the ack while HELD).  This module recognizes it directly from the
program's command/transition structure: the operator button whose gated rung
fires **at the current state** and issues a command that a live
``(command==cv, state==current)`` transition consumes — i.e. the one push the
program is dwelling on before it drives itself onward.  It is a bearing: Act
presses it, ``verify_gates`` judges the outcome, and if it was the wrong read the
loop reverts exactly as before.  Fail-closed: if the legal action is not unique,
or is avoided, or the register is not a recognized channel, it returns ``None``
and the loop keeps today's behavior.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from pyrung.core.analysis.pdg import resolve_rung
from pyrung.core.analysis.pilot.types import WalkContext
from pyrung.core.analysis.simplified import _sp_to_expr
from pyrung.core.analysis.sp_values import (
    _extract_condition_values,
    _values_match,
    _written_value_for_tag,
)
from pyrung.core.crossing import Literal


@dataclass(frozen=True)
class WorldView:
    """A minimal :class:`WalkContext` a caller assembles from a live frame.

    ``_PilotContext`` carries every field a ``WalkContext`` names except the
    live ``snapshot`` (which lives on the iteration frame), so candidates.py
    builds one of these from ``frame.snap`` plus the context constants.  It is a
    world-describing view only — no route/recursion control.
    """

    snapshot: Mapping[str, Any]
    pdg: Any
    program: Any
    steerable: frozenset[str]
    opaque_loop: frozenset[str]
    prior: Any = None


@dataclass(frozen=True)
class OperatorAction:
    """The one operator action the program is waiting for at the current state.

    A bearing, not a plan step: ``action`` is the ``(tag, level)`` push, and the
    remaining fields are the recognized ``(state, command, next-state)`` context
    recorded for legibility (every current decision is dumpable).
    """

    action: tuple[str, Any]
    command_tag: str
    command_value: Any
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


def _request_tags_for(ctx: WalkContext, channel_tag: str, pipeline_roles: Any) -> frozenset[str]:
    tags: set[str] = set()
    for role in pipeline_roles or ():
        if role.channel_tag == channel_tag:
            tags |= set(role.request_tags)
    return frozenset(tags)


def operator_action_for_state(
    ctx: WalkContext,
    channel_tag: str,
    pipeline_roles: Any,
    *,
    avoid_pred: Any = None,
) -> OperatorAction | None:
    """The one operator action the program is dwelling on at the current state.

    Returns ``None`` (today's behavior) unless there is exactly one non-avoided
    operator button whose rung fires now and drives a live command-gated
    transition off the current state — the fail-closed / legible contract.
    """
    snapshot = dict(ctx.snapshot)
    state_value = snapshot.get(channel_tag)
    if state_value is None:
        return None

    request_tags = _request_tags_for(ctx, channel_tag, pipeline_roles)
    transitions = _state_transitions(ctx, channel_tag, state_value, request_tags)
    if not transitions:
        return None

    candidates: list[OperatorAction] = []
    seen_buttons: set[str] = set()
    for button in sorted(ctx.steerable):
        if button in seen_buttons:
            continue
        action = (button, True)
        if avoid_pred is not None and _action_avoided(action, snapshot, avoid_pred):
            continue
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
            candidates.append(
                OperatorAction(
                    action=action,
                    command_tag=next(iter(transition.command_guards), ""),
                    command_value=writes.get(next(iter(transition.command_guards), "")),
                    from_state=state_value,
                    to_state=transition.to_value,
                    note=(
                        f"program-owned current: {channel_tag}={state_value!r} awaits "
                        f"{button} ({command_desc}) -> {channel_tag}={transition.to_value!r}"
                    ),
                )
            )
            break

    # Fail-closed: only a *unique* legal push is a bearing.  Ambiguity punts to
    # today's behavior (no fabricated choice).
    if len(candidates) != 1:
        return None
    return candidates[0]


def _action_avoided(action: tuple[str, Any], snapshot: dict[str, Any], avoid_pred: Any) -> bool:
    """Whether pressing *action* would trip the avoid predicate on the overlay."""
    overlay = {**snapshot, action[0]: action[1]}
    try:
        return bool(avoid_pred(overlay))
    except Exception:
        return False
