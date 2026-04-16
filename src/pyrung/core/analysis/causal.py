"""Retrospective causal chain analysis for pyrung programs.

Walks recorded scan history backward from a tag transition, using per-rung
SP-tree attribution to identify which contacts mattered, and classifies
them as proximate causes (transitioned) or enabling conditions (held steady).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Literal

from pyrung.core.analysis.sp_tree import attribute

if TYPE_CHECKING:
    from pyrung.core.condition import Condition
    from pyrung.core.history import History
    from pyrung.core.rung import Rung
    from pyrung.core.tag import Tag


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Transition:
    """A tag value change at a specific scan."""

    tag_name: str
    scan_id: int
    from_value: Any
    to_value: Any

    def to_dict(self) -> dict[str, Any]:
        return {
            "tag": self.tag_name,
            "scan": self.scan_id,
            "from": self.from_value,
            "to": self.to_value,
        }


@dataclass(frozen=True)
class EnablingCondition:
    """A contact that held the path open but didn't transition."""

    tag_name: str
    value: Any
    held_since_scan: int | None  # None if never changed in retained history

    def to_dict(self) -> dict[str, Any]:
        return {
            "tag": self.tag_name,
            "value": self.value,
            "held_since_scan": self.held_since_scan,
        }


@dataclass(frozen=True)
class ChainStep:
    """One causal link: a rung fired and wrote a tag.

    ``transition`` is the tag change produced by this rung.
    ``proximate_causes`` are inputs that transitioned (what flipped the rung).
    ``enabling_conditions`` are inputs that held steady (required but didn't change).
    """

    transition: Transition
    rung_index: int
    proximate_causes: tuple[Transition, ...]
    enabling_conditions: tuple[EnablingCondition, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "transition": self.transition.to_dict(),
            "rung_index": self.rung_index,
            "proximate_causes": [t.to_dict() for t in self.proximate_causes],
            "enabling_conditions": [e.to_dict() for e in self.enabling_conditions],
        }


@dataclass
class CausalChain:
    """Result of causal chain analysis.

    ``effect`` is the transition being explained.
    ``steps`` are ordered from effect backward toward root causes.
    ``conjunctive_roots`` are root inputs that fired together (AND — joint causation).
    ``ambiguous_roots`` are root inputs we can't disambiguate (OR — genuine uncertainty).
    ``confidence`` is 1.0 when unambiguous; ``1 / len(ambiguous_roots)`` otherwise.
    """

    effect: Transition
    mode: Literal["retrospective", "prospective"]
    steps: list[ChainStep] = field(default_factory=list)
    conjunctive_roots: list[Transition] = field(default_factory=list)
    ambiguous_roots: list[Transition] = field(default_factory=list)

    @property
    def confidence(self) -> float:
        if not self.ambiguous_roots:
            return 1.0
        return 1.0 / len(self.ambiguous_roots)

    @property
    def duration_scans(self) -> int:
        if not self.steps:
            return 0
        scan_ids = [s.transition.scan_id for s in self.steps]
        return self.effect.scan_id - min(scan_ids)

    def tags(self) -> list[str]:
        """All unique tag names appearing in the chain."""
        seen: set[str] = set()
        result: list[str] = []

        def _add(name: str) -> None:
            if name not in seen:
                seen.add(name)
                result.append(name)

        _add(self.effect.tag_name)
        for step in self.steps:
            _add(step.transition.tag_name)
            for pc in step.proximate_causes:
                _add(pc.tag_name)
            for ec in step.enabling_conditions:
                _add(ec.tag_name)
        for t in self.conjunctive_roots:
            _add(t.tag_name)
        for t in self.ambiguous_roots:
            _add(t.tag_name)
        return result

    def rungs(self) -> list[int]:
        """Unique rung indices in chain order."""
        seen: set[int] = set()
        result: list[int] = []
        for step in self.steps:
            if step.rung_index not in seen:
                seen.add(step.rung_index)
                result.append(step.rung_index)
        return result

    def to_dict(self) -> dict[str, Any]:
        """Rich serialization for UI / LLM consumption."""
        return {
            "effect": self.effect.to_dict(),
            "mode": self.mode,
            "steps": [s.to_dict() for s in self.steps],
            "conjunctive_roots": [t.to_dict() for t in self.conjunctive_roots],
            "ambiguous_roots": [t.to_dict() for t in self.ambiguous_roots],
            "confidence": self.confidence,
            "duration_scans": self.duration_scans,
        }

    def to_config(self) -> dict[str, Any]:
        """Round-trippable compact serialization for DAP / presets."""
        return {
            "effect": self.effect.tag_name,
            "scan": self.effect.scan_id,
            "mode": self.mode,
            "steps": [
                {
                    "tag": s.transition.tag_name,
                    "scan": s.transition.scan_id,
                    "rung": s.rung_index,
                }
                for s in self.steps
            ],
            "confidence": self.confidence,
        }


# ---------------------------------------------------------------------------
# Helpers for evaluating conditions against historical state
# ---------------------------------------------------------------------------


class _HistoricalView:
    """Duck-typed evaluator for conditions against a historical SystemState.

    Conditions call ``ctx.get_tag()`` and ``ctx.get_memory()``.  This provides
    those methods backed by a frozen SystemState snapshot.
    """

    __slots__ = ("_state",)

    def __init__(self, state: Any) -> None:
        self._state = state

    def get_tag(self, name: str, default: Any = None) -> Any:
        val = self._state.tags.get(name)
        return val if val is not None else default

    def get_memory(self, key: str, default: Any = None) -> Any:
        val = self._state.memory.get(key)
        return val if val is not None else default


def _condition_tag_name(condition: Condition) -> str | None:
    """Extract the primary tag name from a leaf condition, or None."""
    tag = getattr(condition, "tag", None)
    if tag is None:
        return None
    # Handle ImmediateRef wrapping (check class name to avoid triggering
    # Tag.value property which requires an active runner)
    from pyrung.core.tag import ImmediateRef

    if isinstance(tag, ImmediateRef):
        inner = object.__getattribute__(tag, "value")
        return getattr(inner, "name", None)
    return getattr(tag, "name", None)


# ---------------------------------------------------------------------------
# History walking helpers
# ---------------------------------------------------------------------------


def _scan_ids_descending(history: History) -> list[int]:
    """Return retained scan ids newest-first."""
    return list(reversed(history._order))


def _find_transition(
    history: History,
    tag_name: str,
    scan_id: int | None = None,
) -> Transition | None:
    """Find a transition of *tag_name* in retained history.

    If *scan_id* is given, check whether the tag changed at that exact scan.
    Otherwise find the most recent transition.
    """
    ids = list(history._order)

    if scan_id is not None:
        idx = None
        for i, sid in enumerate(ids):
            if sid == scan_id:
                idx = i
                break
        if idx is None:
            return None
        state = history.at(scan_id)
        to_value = state.tags.get(tag_name)
        if idx > 0:
            prev_state = history.at(ids[idx - 1])
            from_value = prev_state.tags.get(tag_name)
        else:
            # First retained scan — treat default as from_value
            from_value = None
        if from_value != to_value:
            return Transition(tag_name, scan_id, from_value, to_value)
        return None

    # Walk backward to find most recent transition
    for i in range(len(ids) - 1, 0, -1):
        cur_state = history.at(ids[i])
        prev_state = history.at(ids[i - 1])
        cur_val = cur_state.tags.get(tag_name)
        prev_val = prev_state.tags.get(tag_name)
        if cur_val != prev_val:
            return Transition(tag_name, ids[i], prev_val, cur_val)
    return None


def _find_transition_at_scan(
    history: History,
    tag_name: str,
    scan_id: int,
) -> Transition | None:
    """Check if *tag_name* transitioned at exactly *scan_id*."""
    return _find_transition(history, tag_name, scan_id=scan_id)


def _find_last_transition_scan(
    history: History,
    tag_name: str,
    before_scan_id: int,
) -> int | None:
    """Find the most recent scan where *tag_name* changed, before *before_scan_id*.

    Returns the scan_id, or None if no transition found in retained history.
    """
    ids = list(history._order)
    for i in range(len(ids) - 1, 0, -1):
        if ids[i] >= before_scan_id:
            continue
        cur_val = history.at(ids[i]).tags.get(tag_name)
        prev_val = history.at(ids[i - 1]).tags.get(tag_name)
        if cur_val != prev_val:
            return ids[i]
    return None


def _find_recent_transition(
    history: History,
    tag_name: str,
    scan_id: int,
) -> Transition | None:
    """Find a transition of *tag_name* at *scan_id* or the immediately preceding scan.

    PLC effects propagate one scan at a time: a contact that transitioned at
    scan N may not affect a downstream rung until scan N+1 (if the reading
    rung comes before the writing rung in program order).  Checking both the
    current and previous scan captures this one-scan propagation delay.
    """
    # Check exact scan first
    t = _find_transition_at_scan(history, tag_name, scan_id)
    if t is not None:
        return t

    # Check immediately preceding scan
    ids = list(history._order)
    idx = None
    for i, sid in enumerate(ids):
        if sid == scan_id:
            idx = i
            break
    if idx is not None and idx > 0:
        prev_scan = ids[idx - 1]
        t = _find_transition_at_scan(history, tag_name, prev_scan)
        if t is not None:
            return t

    return None


# ---------------------------------------------------------------------------
# Retrospective backward walk
# ---------------------------------------------------------------------------


def retrospective_cause(
    logic: list[Rung],
    history: History,
    rung_firings_fn: Any,  # Callable[[int], PMap]
    tag: Tag | str,
    scan_id: int | None = None,
) -> CausalChain | None:
    """Build a retrospective causal chain for a tag transition.

    Args:
        logic: The program's rung list (``plc._logic``).
        history: The runner's ``History`` instance.
        rung_firings_fn: Callable that returns ``PMap[int, PMap[str, Any]]``
            for a given scan_id.
        tag: The tag (or tag name) whose transition to explain.
        scan_id: Specific scan to examine, or ``None`` for most recent.

    Returns:
        A ``CausalChain``, or ``None`` if no transition was found.
    """
    tag_name = tag if isinstance(tag, str) else tag.name

    transition = _find_transition(history, tag_name, scan_id)
    if transition is None:
        return None

    steps: list[ChainStep] = []
    conjunctive_roots: list[Transition] = []
    ambiguous_roots: list[Transition] = []
    visited: set[str] = set()

    _walk_backward(
        logic=logic,
        history=history,
        rung_firings_fn=rung_firings_fn,
        transition=transition,
        steps=steps,
        conjunctive_roots=conjunctive_roots,
        ambiguous_roots=ambiguous_roots,
        visited=visited,
    )

    return CausalChain(
        effect=transition,
        mode="retrospective",
        steps=steps,
        conjunctive_roots=conjunctive_roots,
        ambiguous_roots=ambiguous_roots,
    )


def _walk_backward(
    *,
    logic: list[Rung],
    history: History,
    rung_firings_fn: Any,
    transition: Transition,
    steps: list[ChainStep],
    conjunctive_roots: list[Transition],
    ambiguous_roots: list[Transition],
    visited: set[str],
) -> None:
    """Recursive backward walk from a single transition."""
    tag_name = transition.tag_name
    scan_id = transition.scan_id

    if tag_name in visited:
        return  # cycle guard
    visited.add(tag_name)

    # Find rungs that wrote this tag at this scan
    firings = rung_firings_fn(scan_id)
    writing_rungs: list[int] = []
    for rung_idx in firings:
        writes = firings[rung_idx]
        if tag_name in writes and writes[tag_name] == transition.to_value:
            writing_rungs.append(rung_idx)

    if not writing_rungs:
        # No rung wrote this value — root cause (external input / patch)
        conjunctive_roots.append(transition)
        return

    for rung_idx in writing_rungs:
        rung = logic[rung_idx]
        sp_tree = rung.sp_tree()

        if sp_tree is None:
            # Unconditional rung — no conditions to attribute
            steps.append(
                ChainStep(
                    transition=transition,
                    rung_index=rung_idx,
                    proximate_causes=(),
                    enabling_conditions=(),
                )
            )
            conjunctive_roots.append(transition)
            continue

        # Evaluate the SP tree against the historical state at this scan
        state = history.at(scan_id)
        view = _HistoricalView(state)

        def _eval(cond: Condition, _v: Any = view) -> bool:
            return cond.evaluate(_v)  # type: ignore[arg-type]

        attributions = attribute(sp_tree, _eval)

        # Classify each attributed contact
        proximate: list[Transition] = []
        enabling: list[EnablingCondition] = []

        for attr in attributions:
            cond_tag = _condition_tag_name(attr.condition)
            if cond_tag is None:
                continue

            cond_transition = _find_recent_transition(history, cond_tag, scan_id)
            if cond_transition is not None:
                proximate.append(cond_transition)
            else:
                held_since = _find_last_transition_scan(history, cond_tag, scan_id)
                enabling.append(
                    EnablingCondition(
                        tag_name=cond_tag,
                        value=state.tags.get(cond_tag),
                        held_since_scan=held_since,
                    )
                )

        steps.append(
            ChainStep(
                transition=transition,
                rung_index=rung_idx,
                proximate_causes=tuple(proximate),
                enabling_conditions=tuple(enabling),
            )
        )

        if not proximate:
            # All contacts were enabling — the transition itself is a root
            conjunctive_roots.append(transition)
        else:
            # Recurse on each proximate cause
            for p in proximate:
                _walk_backward(
                    logic=logic,
                    history=history,
                    rung_firings_fn=rung_firings_fn,
                    transition=p,
                    steps=steps,
                    conjunctive_roots=conjunctive_roots,
                    ambiguous_roots=ambiguous_roots,
                    visited=visited,
                )
