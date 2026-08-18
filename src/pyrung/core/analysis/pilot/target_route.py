"""Selection, reporting, and obstruction diagnosis for a target route."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from pyrung.core.analysis.graph import RouteAlt, RoutePivot, RouteTaken
from pyrung.core.analysis.pilot.trace import trace_back
from pyrung.core.analysis.pilot.trace_read import (
    TraceChoice,
    TraceReadConstraints,
    UnsupportedConstruct,
)
from pyrung.core.analysis.pilot.trace_routes import rank_trace_choices
from pyrung.core.analysis.sp_values import _values_match
from pyrung.core.analysis.steerable import compute_clear_only

if TYPE_CHECKING:
    from pyrung.core.analysis.pdg import ProgramGraph
    from pyrung.core.runner import PLC


# ---------------------------------------------------------------------------
# Failure diagnostics
# ---------------------------------------------------------------------------


def harness_couplings(plc: PLC) -> tuple[tuple[str, str], ...]:
    """The ``(en, fb)`` pairs the Harness still synthesizes on *plc*, for the
    linked-feedback diagnostic.  Empty when there is no harness (no couplings)
    or every coupling was ``unlink``-ed away."""
    harness = getattr(plc, "_harness", None)
    if harness is None:
        return ()
    return tuple((c.en_name, c.fb_name) for c in harness.couplings())


def linked_feedback_block(
    target_tag: str,
    target_value: Any,
    snapshot: dict[str, Any],
    pdg: ProgramGraph,
    program: Any,
    steerable: frozenset[str],
    couplings: tuple[tuple[str, str], ...],
) -> str | None:
    """Honest diagnostic for an unreachable target gated by a harness link.

    When the target's backward-trace route contains both a synthesized feedback
    tag ``fb`` *and* its driver ``en`` (the ``link=`` source), the Harness holds
    ``fb`` lockstep with ``en`` — so the moment the route drives ``en`` to its
    active value, the link drives ``fb`` to the opposite of what the route needs
    (valve open ⇒ flow sensor reads active, defeating the "no flow" watchdog).
    PILOT may not steer ``fb`` (the Harness owns it), so the target is
    unreachable until the link is defeated.  Returns a message naming the
    offending link(s) and the ``unlink=`` override, or ``None`` if no link gates
    the route (then the caller falls back to the generic budget reason).
    """
    if not couplings:
        return None
    try:
        tree = trace_back(target_tag, target_value, snapshot, pdg, program, steerable)
    except UnsupportedConstruct:
        raise
    except Exception:  # noqa: BLE001 — diagnostic only; never mask the real failure
        return None
    route_tags = {n.tag for n in tree.iter_nodes()}
    blockers = [
        (en, fb)
        for en, fb in couplings
        if fb in route_tags and en in route_tags and fb not in steerable
    ]
    if not blockers:
        return None
    links = ", ".join(f"{fb}<-{en}" for en, fb in blockers)
    names = ", ".join(repr(fb) for _en, fb in blockers)
    return (
        f"pilot: {target_tag}={target_value!r} is blocked by physical link(s) "
        f"{links}; the harness holds the sensor lockstep with its driver, so it "
        f"cannot rest at the value this route needs. Retry with unlink=[{names}] "
        f"to model a dead sensor (fault injection)."
    )


def target_is_value_route(target_predicate: Any) -> bool:
    """Does this target get route enumeration?

    Any concrete equality target — ``Bool == True``, ``Bool == False``, or a
    word ``tag == value`` — is a frozen value the route machinery can enumerate
    writers/OR-arms for (``_can_produce`` against that value).  A live relational
    predicate (``State > 5``) is *not*: its goal is the relation, not a frozen
    value, so ``target_value`` is only a display representative and there is no
    producible-value writer set to route over.  Those targets flow unlocked and
    are honestly reported without a ``RouteTaken``.
    """
    return target_predicate is None


def _route_name(route: TraceChoice) -> str:
    """Human name for a route."""
    if route.route_condition is not None:
        tag, value = route.route_condition
        return tag if value is True else f"{tag}=={value!r}"
    return route.label


def _build_route_taken(
    default: TraceChoice,
    survivors: tuple[TraceChoice, ...],
    steerable: frozenset[str],
) -> RouteTaken:
    """Describe the chosen *default* route plus the routes not taken.

    Models the fork as one pivot whose ``alternatives`` are the other surviving
    routes. ``salient`` is True when any route in the fork is
    gated by a non-steerable discriminator (an internal coil/state the engineer
    commits to) — the trivial all-input fork (``Or(Auto, Manual)``) stays
    non-salient and hidden from the headline.
    """
    others = tuple(ch for ch in survivors if ch.id != default.id)
    alternatives = tuple(RouteAlt(label=_route_name(ch)) for ch in others)
    conditions = [default.route_condition, *(ch.route_condition for ch in others)]
    salient = any(
        condition is not None and condition[0] not in steerable for condition in conditions
    )
    dtag, dvalue = (
        default.route_condition if default.route_condition is not None else (default.label, True)
    )
    pivot = RoutePivot(
        tag=dtag,
        value=dvalue,
        label=_route_name(default),
        kind="writer" if default.writer_locks else "or-arm",
        avoid_hint=default.route_condition,
        alternatives=alternatives,
        salient=salient,
    )
    return RouteTaken(
        label=_route_name(default),
        pivots=(pivot,),
        dominant=len(survivors) <= 1,
    )


def report_selected_route(
    prepared: RouteTaken | None,
    selected: TraceChoice | None,
) -> RouteTaken | None:
    """Make the public route receipt name the route that actually finished.

    ``prepared`` describes the initially preferred fork so the engineer can see
    its alternatives before execution. If the route that ultimately reaches the
    target differs, rotate the same root pivot around that result. This is
    reporting only; no alternative list feeds back into navigation.
    """

    if prepared is None or selected is None or not prepared.pivots:
        return prepared
    selected_name = _route_name(selected)
    pivot = prepared.pivots[0]
    if pivot.label == selected_name:
        return prepared

    alternatives = [
        RouteAlt(label=pivot.label),
        *(alt for alt in pivot.alternatives if alt.label != selected_name),
    ]
    selected_condition = selected.route_condition
    selected_tag, selected_value = (
        selected_condition if selected_condition is not None else (selected.label, True)
    )
    return RouteTaken(
        label=selected_name,
        pivots=(
            RoutePivot(
                tag=selected_tag,
                value=selected_value,
                label=selected_name,
                kind="writer" if selected.writer_locks else "or-arm",
                avoid_hint=selected_condition,
                alternatives=tuple(alternatives),
                salient=pivot.salient,
            ),
        ),
        dominant=prepared.dominant,
    )


def prepare_target_route(
    plc: PLC,
    target_tag: str,
    target_value: Any,
    pdg: ProgramGraph,
    program: Any,
    steerable: frozenset[str],
    opaque_loop: frozenset[str],
    *,
    target_predicate: Any = None,
    avoid_pred: Any = None,
) -> RouteTaken | None:
    """Describe the preferred current-world route.

    Works for any concrete equality target — ``Bool == True``, ``Bool == False``,
    or a word ``tag == value``; a live relational predicate gets no route (see
    :func:`target_is_value_route`).  ``how()`` never reports ambiguous: it
    enumerates the routes, prunes any that ``avoid=`` forbids, ranks the
    cheapest survivor (gate-eligible routes preferred, trace score next, rung
    order breaking ties), and records the alternatives on the returned
    :class:`RouteTaken`. Execution remains unlocked so every current-world read
    can choose any admissible root route.
    """
    snapshot = dict(plc.state.tags)
    if not (
        target_is_value_route(target_predicate)
        and not _values_match(snapshot.get(target_tag), target_value)
    ):
        return None
    clear_only = compute_clear_only(pdg, plc._known_tags_by_name, program)
    choices, traced = rank_trace_choices(
        target_tag,
        target_value,
        snapshot,
        pdg,
        program,
        steerable,
        constraints=TraceReadConstraints(
            clear_only=clear_only,
            opaque_loop=opaque_loop,
            avoid_pred=avoid_pred,
        ),
    )
    if not choices:
        return None
    if not traced:
        return None
    default = traced[0][0]
    survivors = tuple(choice for choice, _tree in traced)
    return _build_route_taken(default, survivors, steerable)
