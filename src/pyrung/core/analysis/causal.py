"""Causal chain analysis for pyrung programs.

Recorded backward walk (``cause``): from a tag transition, walk history
backward using per-rung SP-tree attribution to find proximate causes and
enabling conditions.

Recorded forward walk (``effect``): from a tag transition, walk history
forward using counterfactual SP evaluation to find downstream effects.

Projected walks (``cause(to=)``, ``effect(from_=)``): project from the
current state using the static PDG to find reachable causal paths.
Returns ``mode='projected'`` when reachable, ``mode='unreachable'`` with
populated ``blockers`` when not.

PDG fallback for filtered firing logs
-------------------------------------

The recorded walks consume the rung-firings log via ``rung_firings_fn``.
Under PDG-filtered capture (see ``context.py::capturing_rung``), writes
to non-Bool tags that no rung reads are dropped from the log at
capture time — the filter saves memory on internal churn like timer
accumulators.  Bool tags are always kept regardless of read status
(low cardinality, user-facing state transitions, common target of
``cause()``) so the direct-log path handles the typical case.

The filter's *downstream* claim (a write that no rung reads can't
matter to analysis) holds for the **recursive** walk step: once we
identify the writing rung, its causes are reads by upstream rungs,
all of which are consumed-and-therefore-logged by definition.  The
filter's claim **fails at the root step** whenever the analysis
target is a terminal **non-Bool** output — e.g. ``cause("Timer_Acc")``
where nothing reads the accumulator.  (Terminal Bool outputs like
``Alarm_Horn`` are preserved by the Bool-keep rule and hit the
direct-log path.)  Without a fallback, the firing log would lack
the rung that wrote the non-Bool terminal, and the chain's first
step would never materialize.

The fix is a PDG fallback keyed off the static ``writers_of`` /
``readers_of`` sets.  When the firing log doesn't identify a writer
for a transition, :func:`_fallback_writers_from_pdg` iterates the
PDG's static writers and re-evaluates each candidate's SP-tree
against the historical state at that scan.  A candidate whose tree
evaluates True is treated as the writer.  Symmetric logic widens
the effect forward walk: for each scan, rungs missing from the log
but reading a current frontier tag are re-entered and evaluated
with PDG-synthesized candidate writes (history then filters via
``_find_transition_at_scan``).

Trade-off: the fallback adds one SP-tree eval per candidate rung
per unresolved step — bounded by ``len(writers_of[tag])``, typically
1–2.  Memory/correctness both preserved; the filter's cache miss
turns into a handful of extra evaluations, not a lost answer.

``FiredOnly`` rungs deliberately do *not* round-trip through
``cause``'s value match: their synthesized writes carry a sentinel
that never equals a real transition value.  Such rungs drop out of
recorded backward chains past their promotion point.  The
assumption is monotonic counters don't carry useful causal signal;
analysis that truly needs the value replays to the scan.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import TYPE_CHECKING, Any, Literal

from pyrung.core.analysis.sp_tree import SPNode, attribute, evaluate_sp

if TYPE_CHECKING:
    from pyrung.core.analysis.pdg import ProgramGraph
    from pyrung.core.condition import Condition
    from pyrung.core.history import History
    from pyrung.core.rung import Rung
    from pyrung.core.rung_firings import RungFiringTimelines
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


class BlockerReason(Enum):
    """Why a projected path is unreachable."""

    NO_OBSERVED_TRANSITION = "NO_OBSERVED_TRANSITION"
    BLOCKED_UPSTREAM = "BLOCKED_UPSTREAM"
    STRUCTURAL_CONTRADICTION = "STRUCTURAL_CONTRADICTION"


@dataclass(frozen=True)
class BlockingCondition:
    """A contact that would need to transition but can't be reached.

    Populated when ``CausalChain.mode == 'unreachable'``.
    """

    rung_index: int
    blocked_tag: str
    needed_value: Any
    reason: BlockerReason
    sub_blockers: tuple[BlockingCondition, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {
            "rung_index": self.rung_index,
            "blocked_tag": self.blocked_tag,
            "needed_value": self.needed_value,
            "reason": self.reason.value,
        }
        if self.sub_blockers:
            d["sub_blockers"] = [b.to_dict() for b in self.sub_blockers]
        return d


@dataclass(frozen=True)
class ChainStep:
    """One causal link: a rung fired and wrote a tag.

    ``transition`` is the tag change produced by this rung.
    ``proximate_causes`` are inputs that transitioned (what flipped the rung).
    ``enabling_conditions`` are inputs that held steady (required but didn't change).
    ``fidelity`` is ``"full"`` when SP-tree attribution was used (state
    was cached), or ``"timeline"`` when only structural + timeline
    data was available (cache miss — ``enabling_conditions`` will be
    empty and ``proximate_causes`` is a superset of the true set).
    """

    transition: Transition
    rung_index: int
    proximate_causes: tuple[Transition, ...]
    enabling_conditions: tuple[EnablingCondition, ...]
    fidelity: Literal["full", "timeline"] = "full"

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {
            "transition": self.transition.to_dict(),
            "rung_index": self.rung_index,
            "proximate_causes": [t.to_dict() for t in self.proximate_causes],
            "enabling_conditions": [e.to_dict() for e in self.enabling_conditions],
        }
        if self.fidelity != "full":
            d["fidelity"] = self.fidelity
        return d


@dataclass
class CausalChain:
    """Result of causal chain analysis.

    ``effect`` is the transition being explained (or projected/shown unreachable).
    ``mode`` is ``'recorded'``, ``'projected'``, or ``'unreachable'``.
    ``steps`` are ordered from effect backward toward root causes.
    ``conjunctive_roots`` are root inputs that fired together (AND — joint causation).
    ``ambiguous_roots`` are root inputs we can't disambiguate (OR — genuine uncertainty).
    ``blockers`` are populated when ``mode == 'unreachable'`` — the contacts
    that would need to transition but can't be reached.
    ``confidence`` is 1.0 when unambiguous; ``1 / len(ambiguous_roots)`` otherwise.
    """

    effect: Transition
    mode: Literal["recorded", "projected", "unreachable"]
    steps: list[ChainStep] = field(default_factory=list)
    conjunctive_roots: list[Transition] = field(default_factory=list)
    ambiguous_roots: list[Transition] = field(default_factory=list)
    blockers: list[BlockingCondition] = field(default_factory=list)

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
        all_scans = scan_ids + [self.effect.scan_id]
        return max(all_scans) - min(all_scans)

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
        d: dict[str, Any] = {
            "effect": self.effect.to_dict(),
            "mode": self.mode,
            "steps": [s.to_dict() for s in self.steps],
            "conjunctive_roots": [t.to_dict() for t in self.conjunctive_roots],
            "ambiguous_roots": [t.to_dict() for t in self.ambiguous_roots],
            "confidence": self.confidence,
            "duration_scans": self.duration_scans,
        }
        if self.blockers:
            d["blockers"] = [b.to_dict() for b in self.blockers]
        return d

    def to_config(self) -> dict[str, Any]:
        """Round-trippable compact serialization for DAP / presets."""
        steps: list[dict[str, Any]] = []
        for s in self.steps:
            entry: dict[str, Any] = {
                "tag": s.transition.tag_name,
                "scan": s.transition.scan_id,
                "rung": s.rung_index,
            }
            if s.fidelity != "full":
                entry["fidelity"] = s.fidelity
            steps.append(entry)
        return {
            "effect": self.effect.tag_name,
            "scan": self.effect.scan_id,
            "mode": self.mode,
            "steps": steps,
            "confidence": self.confidence,
        }

    def __str__(self) -> str:
        """Human-readable chain report."""
        e = self.effect
        lines: list[str] = []

        if self.mode == "unreachable":
            lines.append(f"{e.tag_name} → {e.to_value!r}  [unreachable]")
            for b in self.blockers:
                lines.append(
                    f"  Rung {b.rung_index} would clear, but {b.blocked_tag} is unreachable"
                )
                lines.append(f"    reason: {b.reason.value}")
            return "\n".join(lines)

        mode_label = self.mode
        if self.mode == "projected":
            lines.append(f"{e.tag_name} → {e.to_value!r}  [{mode_label}]")
        else:
            lines.append(
                f"{e.tag_name} {e.from_value!r}→{e.to_value!r} at scan {e.scan_id}  [{mode_label}]"
            )

        for step in self.steps:
            t = step.transition
            fidelity_note = ""
            if step.fidelity == "timeline":
                fidelity_note = "  (partial; re-run with scan_id for full fidelity)"
            lines.append(f"  Rung {step.rung_index}: {t.tag_name} → {t.to_value!r}{fidelity_note}")
            for pc in step.proximate_causes:
                lines.append(f"    proximate: {pc.tag_name} {pc.from_value!r}→{pc.to_value!r}")
            if step.fidelity == "full":
                for ec in step.enabling_conditions:
                    lines.append(f"    enabling:  {ec.tag_name} = {ec.value!r}")

        return "\n".join(lines)


# ---------------------------------------------------------------------------
# SP tree leaf collection (for projected walks)
# ---------------------------------------------------------------------------


def _collect_sp_leaves(node: SPNode) -> list[Any]:
    """Collect all ``SPLeaf`` conditions from an SP tree, regardless of evaluation."""
    from pyrung.core.analysis.sp_tree import SPLeaf, SPParallel, SPSeries

    if isinstance(node, SPLeaf):
        return [node]
    result: list[SPLeaf] = []
    if isinstance(node, (SPSeries, SPParallel)):
        for child in node.children:
            result.extend(_collect_sp_leaves(child))
    return result


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
    """Return addressable scan ids newest-first."""
    return list(reversed(list(history.scan_ids())))


def _find_transition(
    history: History,
    tag_name: str,
    scan_id: int | None = None,
    *,
    timelines: RungFiringTimelines | None = None,
    pdg: ProgramGraph | None = None,
) -> Transition | None:
    """Find a transition of *tag_name* in addressable history.

    If *scan_id* is given, check whether the tag changed at that exact scan.
    Otherwise find the most recent transition.

    When *timelines* and *pdg* are provided, uses the firing timeline
    instead of per-scan state reads — O(W × log S) where W is the
    number of writer rungs for the tag.
    """
    ids = list(history.scan_ids())

    if scan_id is not None:
        return _find_transition_at_scan(
            history,
            tag_name,
            scan_id,
            timelines=timelines,
            pdg=pdg,
        )

    # Walk backward to find most recent transition.
    # Timeline path: check each scan for a writer that changed the value.
    writers = _writer_indices(pdg, tag_name) if pdg is not None else None
    if timelines is not None and writers is not None and writers:
        # Walk backward through scans using the timeline for value checks.
        for i in range(len(ids) - 1, 0, -1):
            cur_val = _tag_value_at_scan(timelines, writers, tag_name, ids[i])
            prev_val = _tag_value_at_scan(timelines, writers, tag_name, ids[i - 1])
            if cur_val is not _NO_WRITE and prev_val is not _NO_WRITE and cur_val != prev_val:
                return Transition(tag_name, ids[i], prev_val, cur_val)
            if cur_val is not _NO_WRITE and prev_val is _NO_WRITE:
                # No rung wrote the tag at the previous scan — fall
                # back to state to get the prior value (could be a
                # default or an external input).
                prev_state_val = history.at(ids[i - 1]).tags.get(tag_name)
                if cur_val != prev_state_val:
                    return Transition(tag_name, ids[i], prev_state_val, cur_val)
        # Timeline didn't find a write — may be PDG-filtered.
        # Fall through to state reads.

    # State-based fallback: external inputs (no writers), PDG-filtered
    # writes, or no timeline available.
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
    *,
    timelines: RungFiringTimelines | None = None,
    pdg: ProgramGraph | None = None,
) -> Transition | None:
    """Check if *tag_name* transitioned at exactly *scan_id*.

    Timeline path avoids state reads by checking writer firings.
    """
    ids = list(history.scan_ids())
    idx = None
    for i, sid in enumerate(ids):
        if sid == scan_id:
            idx = i
            break
    if idx is None:
        return None

    writers = _writer_indices(pdg, tag_name) if pdg is not None else None
    if timelines is not None and writers is not None and writers:
        to_value = _tag_value_at_scan(timelines, writers, tag_name, scan_id)
        if to_value is not _NO_WRITE:
            if idx > 0:
                prev_result = timelines.last_tag_write_before(writers, tag_name, scan_id)
                if prev_result is not None:
                    from_value = prev_result[1]
                else:
                    from_value = history.at(ids[idx - 1]).tags.get(tag_name)
            else:
                from_value = None
            if from_value != to_value:
                return Transition(tag_name, scan_id, from_value, to_value)
            return None
        # _NO_WRITE — fall through to state reads (PDG-filtered or
        # external input).

    # State-based fallback
    state = history.at(scan_id)
    to_value = state.tags.get(tag_name)
    if idx > 0:
        prev_state = history.at(ids[idx - 1])
        from_value = prev_state.tags.get(tag_name)
    else:
        from_value = None
    if from_value != to_value:
        return Transition(tag_name, scan_id, from_value, to_value)
    return None


def _find_last_transition_scan(
    history: History,
    tag_name: str,
    before_scan_id: int,
    *,
    timelines: RungFiringTimelines | None = None,
    pdg: ProgramGraph | None = None,
) -> int | None:
    """Find the most recent scan where *tag_name* changed, before *before_scan_id*.

    Returns the scan_id, or None if no transition found in addressable history.

    Timeline path uses reverse iteration over writer rung timelines —
    O(W × log S) where W is the writer count.
    """
    writers = _writer_indices(pdg, tag_name) if pdg is not None else None
    if timelines is not None and writers is not None and writers:
        # Walk backward via the timeline's range lists.
        ids = list(history.scan_ids())
        for i in range(len(ids) - 1, 0, -1):
            if ids[i] >= before_scan_id:
                continue
            cur_val = _tag_value_at_scan(timelines, writers, tag_name, ids[i])
            if cur_val is _NO_WRITE:
                continue
            prev_val = _tag_value_at_scan(timelines, writers, tag_name, ids[i - 1])
            if prev_val is _NO_WRITE:
                prev_val = history.at(ids[i - 1]).tags.get(tag_name)
            if cur_val != prev_val:
                return ids[i]
        return None

    # State-based fallback (also used for external-input tags with no writers)
    ids = list(history.scan_ids())
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
    *,
    timelines: RungFiringTimelines | None = None,
    pdg: ProgramGraph | None = None,
) -> Transition | None:
    """Find a transition of *tag_name* at *scan_id* or the immediately preceding scan.

    PLC effects propagate one scan at a time: a contact that transitioned at
    scan N may not affect a downstream rung until scan N+1 (if the reading
    rung comes before the writing rung in program order).  Checking both the
    current and previous scan captures this one-scan propagation delay.
    """
    # Check exact scan first
    t = _find_transition_at_scan(
        history,
        tag_name,
        scan_id,
        timelines=timelines,
        pdg=pdg,
    )
    if t is not None:
        return t

    # Check immediately preceding scan
    ids = list(history.scan_ids())
    idx = None
    for i, sid in enumerate(ids):
        if sid == scan_id:
            idx = i
            break
    if idx is not None and idx > 0:
        prev_scan = ids[idx - 1]
        t = _find_transition_at_scan(
            history,
            tag_name,
            prev_scan,
            timelines=timelines,
            pdg=pdg,
        )
        if t is not None:
            return t

    return None


# Sentinel for "no rung wrote this tag at this scan".
_NO_WRITE: Any = object()


def _writer_indices(pdg: ProgramGraph, tag_name: str) -> frozenset[int]:
    """Return the set of rung indices that can write *tag_name*."""
    return pdg.writers_of.get(tag_name, frozenset())


def _tag_value_at_scan(
    timelines: RungFiringTimelines,
    writers: frozenset[int],
    tag_name: str,
    scan_id: int,
) -> Any:
    """Return the value written to *tag_name* at *scan_id*, or ``_NO_WRITE``.

    Checks each writer rung's timeline for a firing at ``scan_id``
    that includes ``tag_name`` in its writes.
    """
    for rung_index in writers:
        writes = timelines.rung_writes_at(rung_index, scan_id)
        if writes is not None and tag_name in writes:
            return writes[tag_name]
    return _NO_WRITE


# ---------------------------------------------------------------------------
# Recorded backward walk
# ---------------------------------------------------------------------------


def recorded_cause(
    logic: list[Rung],
    history: History,
    rung_firings_fn: Any,  # Callable[[int], PMap]
    tag: Tag | str,
    scan_id: int | None = None,
    *,
    pdg: ProgramGraph | None = None,
    timelines: RungFiringTimelines | None = None,
    state_in_cache_fn: Any = None,  # Callable[[int], bool] | None
) -> CausalChain | None:
    """Build a retrospective causal chain for a tag transition.

    Args:
        logic: The program's rung list (``plc._logic``).
        history: The runner's ``History`` instance.
        rung_firings_fn: Callable that returns ``PMap[int, PMap[str, Any]]``
            for a given scan_id.
        tag: The tag (or tag name) whose transition to explain.
        scan_id: Specific scan to examine, or ``None`` for most recent.
        pdg: Static program graph used as a fallback when the firing
            log has been PDG-filtered.  Terminal outputs (tags no rung
            reads) have their rung-firing writes dropped from the log;
            this fallback recovers the writing rung by evaluating each
            candidate from ``writers_of`` against the historical state.
        timelines: Per-rung firing timelines for O(log S) transition
            detection without state reads.

    Returns:
        A ``CausalChain``, or ``None`` if no transition was found.
    """
    tag_name = tag if isinstance(tag, str) else tag.name

    transition = _find_transition(
        history,
        tag_name,
        scan_id,
        timelines=timelines,
        pdg=pdg,
    )
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
        pdg=pdg,
        timelines=timelines,
        state_in_cache_fn=state_in_cache_fn,
    )

    return CausalChain(
        effect=transition,
        mode="recorded",
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
    pdg: ProgramGraph | None = None,
    timelines: RungFiringTimelines | None = None,
    state_in_cache_fn: Any = None,  # Callable[[int], bool] | None
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

    if not writing_rungs and pdg is not None:
        # The firing log has been PDG-filtered — writes to tags no rung
        # reads never landed.  Recover the writer by evaluating each
        # static candidate from ``writers_of`` against the historical
        # state at ``scan_id``.  A rung whose SP tree was true at that
        # scan is treated as the writer; unconditional rungs (no SP
        # tree) always qualify.
        writing_rungs = _fallback_writers_from_pdg(
            pdg=pdg,
            logic=logic,
            history=history,
            tag_name=tag_name,
            scan_id=scan_id,
        )

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

        # Check if state is cached for full-fidelity SP-tree attribution.
        cached = state_in_cache_fn is None or state_in_cache_fn(scan_id)

        if cached:
            # Full fidelity: SP-tree attribution classifies contacts as
            # proximate (transitioned) vs enabling (held steady).
            state = history.at(scan_id)
            view = _HistoricalView(state)

            def _eval(cond: Condition, _v: Any = view) -> bool:
                return cond.evaluate(_v)  # type: ignore[arg-type]

            attributions = attribute(sp_tree, _eval)

            proximate: list[Transition] = []
            enabling: list[EnablingCondition] = []

            for attr in attributions:
                cond_tag = _condition_tag_name(attr.condition)
                if cond_tag is None:
                    continue

                cond_transition = _find_recent_transition(
                    history,
                    cond_tag,
                    scan_id,
                    timelines=timelines,
                    pdg=pdg,
                )
                if cond_transition is not None:
                    proximate.append(cond_transition)
                else:
                    held_since = _find_last_transition_scan(
                        history,
                        cond_tag,
                        scan_id,
                        timelines=timelines,
                        pdg=pdg,
                    )
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
        else:
            # Timeline-only fidelity: no state available, so no SP-tree
            # attribution.  Proximate candidates = rung contacts whose
            # writers fired at N or N-1 (timeline + structural).
            # Enabling conditions are empty (require state).
            proximate_tl: list[Transition] = []
            leaves = _collect_sp_leaves(sp_tree)
            for leaf in leaves:
                cond_tag = _condition_tag_name(leaf.condition)
                if cond_tag is None:
                    continue
                cond_transition = _find_recent_transition(
                    history,
                    cond_tag,
                    scan_id,
                    timelines=timelines,
                    pdg=pdg,
                )
                if cond_transition is not None:
                    proximate_tl.append(cond_transition)

            steps.append(
                ChainStep(
                    transition=transition,
                    rung_index=rung_idx,
                    proximate_causes=tuple(proximate_tl),
                    enabling_conditions=(),
                    fidelity="timeline",
                )
            )
            proximate = proximate_tl  # for recursion check below

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
                    pdg=pdg,
                    timelines=timelines,
                    state_in_cache_fn=state_in_cache_fn,
                )


def _fallback_writers_from_pdg(
    *,
    pdg: ProgramGraph,
    logic: list[Rung],
    history: History,
    tag_name: str,
    scan_id: int,
) -> list[int]:
    """Recover candidate writers of ``tag_name`` at ``scan_id`` from the PDG.

    Used when the firing log has dropped the write under PDG filtering —
    the structural ``writers_of`` set tells us which rungs *can* write
    the tag; re-evaluating each rung's SP tree against the historical
    state narrows to those that *did* fire at ``scan_id``.
    """
    candidates = pdg.writers_of.get(tag_name, frozenset())
    if not candidates:
        return []
    state = history.at(scan_id)
    view = _HistoricalView(state)

    def _eval(cond: Condition, _v: Any = view) -> bool:
        return cond.evaluate(_v)  # type: ignore[arg-type]

    writers: list[int] = []
    for rung_idx in sorted(candidates):
        rung = logic[rung_idx]
        sp_tree = rung.sp_tree()
        if sp_tree is None or evaluate_sp(sp_tree, _eval):
            writers.append(rung_idx)
    return writers


# ---------------------------------------------------------------------------
# Counterfactual SP evaluation
# ---------------------------------------------------------------------------


class _CounterfactualView:
    """Historical view with one tag's value overridden for counterfactual checks.

    Used by the forward walk to answer: "would this rung have evaluated
    the same way if tag X had not transitioned?"
    """

    __slots__ = ("_state", "_override_tag", "_override_value")

    def __init__(self, state: Any, override_tag: str, override_value: Any) -> None:
        self._state = state
        self._override_tag = override_tag
        self._override_value = override_value

    def get_tag(self, name: str, default: Any = None) -> Any:
        if name == self._override_tag:
            return self._override_value if self._override_value is not None else default
        val = self._state.tags.get(name)
        return val if val is not None else default

    def get_memory(self, key: str, default: Any = None) -> Any:
        val = self._state.memory.get(key)
        return val if val is not None else default


def _counterfactual_changes_outcome(
    sp_tree: SPNode,
    state: Any,
    cause_tag: str,
    from_value: Any,
) -> bool:
    """Check if reverting *cause_tag* to *from_value* changes the SP tree outcome.

    Evaluates the tree twice — once with the actual state, once with
    the tag reverted — and returns True if the results differ.
    """
    actual_view = _HistoricalView(state)
    cf_view = _CounterfactualView(state, cause_tag, from_value)

    def _eval_actual(cond: Condition, _v: Any = actual_view) -> bool:
        return cond.evaluate(_v)  # type: ignore[arg-type]

    def _eval_cf(cond: Condition, _v: Any = cf_view) -> bool:
        return cond.evaluate(_v)  # type: ignore[arg-type]

    actual_result = evaluate_sp(sp_tree, _eval_actual)
    cf_result = evaluate_sp(sp_tree, _eval_cf)
    return actual_result != cf_result


# ---------------------------------------------------------------------------
# Recorded forward walk
# ---------------------------------------------------------------------------


def recorded_effect(
    logic: list[Rung],
    history: History,
    rung_firings_fn: Any,  # Callable[[int], PMap]
    tag: Tag | str,
    scan_id: int | None = None,
    *,
    steady_state_k: int = 3,
    max_scans: int = 1000,
    pdg: ProgramGraph | None = None,
    timelines: RungFiringTimelines | None = None,
) -> CausalChain | None:
    """Build a retrospective forward chain from a tag transition.

    Walks history forward from the transition, using counterfactual SP
    evaluation to identify which downstream tags were causally affected.

    Args:
        logic: The program's rung list.
        history: The runner's History instance.
        rung_firings_fn: Returns ``PMap[int, PMap[str, Any]]`` for a scan_id.
        tag: The tag (or tag name) whose downstream effects to trace.
        scan_id: Specific scan of the transition, or ``None`` for most recent.
        steady_state_k: Stop after this many consecutive scans with no new
            tags entering the chain (default 3).
        max_scans: Hard cap on scans to walk forward (default 1000).
        pdg: Static program graph used to widen the per-scan candidate
            rung set when firings are PDG-filtered.  Rungs that fired
            but wrote only unconsumed tags are missing from the log;
            ``readers_of`` recovers them by flagging rungs that read any
            frontier tag.  Downstream tag values are resolved via
            history regardless of whether the rung was in the log.
        timelines: Per-rung firing timelines for O(log S) transition
            detection without state reads.
    """
    tag_name = tag if isinstance(tag, str) else tag.name

    transition = _find_transition(
        history,
        tag_name,
        scan_id,
        timelines=timelines,
        pdg=pdg,
    )
    if transition is None:
        return None

    # Frontier: tags whose downstream effects we're still tracing.
    # Maps tag_name → Transition.
    frontier: dict[str, Transition] = {tag_name: transition}

    steps: list[ChainStep] = []
    seen_effects: set[str] = {tag_name}  # don't re-add the cause itself

    ids = list(history.scan_ids())
    try:
        start_idx = ids.index(transition.scan_id)
    except ValueError:
        return None

    consecutive_empty = 0

    for scan_offset in range(len(ids) - start_idx):
        if scan_offset >= max_scans:
            break

        current_scan = ids[start_idx + scan_offset]
        firings = rung_firings_fn(current_scan)
        new_effects_this_scan = False

        # Iterate rungs in index order: within a single scan the frontier
        # grows as earlier rungs produce effects (e.g. Rung 0 writes
        # Sts_FaultTripped, then Rung 2 reads it), and the per-rung
        # reads-vs-frontier check must be against the *current* frontier.
        # Rungs not in ``firings`` may have fired with all writes dropped
        # by PDG filtering — we consider them only if they statically read
        # some frontier tag, and we synthesize candidate writes from the
        # PDG node so the downstream history lookup can pick up real
        # transitions.
        rung_count = len(logic) if pdg is not None else 0
        rung_range = range(rung_count) if pdg is not None else sorted(firings.keys())

        for rung_idx in rung_range:
            rung = logic[rung_idx]
            if rung_idx in firings:
                writes: Any = firings[rung_idx]
                if not writes and pdg is not None:
                    # Rung fired but filter emptied its writes — synthesize
                    # candidate written tags from the PDG so the history
                    # lookup below can recover real transitions.
                    writes = pdg.rung_nodes[rung_idx].writes
            elif pdg is not None:
                node = pdg.rung_nodes[rung_idx]
                reads = node.condition_reads | node.data_reads
                if reads.isdisjoint(frontier):
                    continue
                writes = node.writes
            else:
                continue
            sp_tree = rung.sp_tree()

            if sp_tree is None:
                continue

            state = history.at(current_scan)

            # Check each frontier tag for counterfactual relevance
            for cause_tag, cause_trans in list(frontier.items()):
                if not _counterfactual_changes_outcome(
                    sp_tree, state, cause_tag, cause_trans.from_value
                ):
                    continue

                # This frontier tag was load-bearing for this rung.
                # Record each new tag transition the rung wrote.
                for written_tag in writes:
                    if written_tag in seen_effects:
                        continue

                    effect_trans = _find_transition_at_scan(
                        history,
                        written_tag,
                        current_scan,
                        timelines=timelines,
                        pdg=pdg,
                    )
                    if effect_trans is None:
                        continue

                    seen_effects.add(written_tag)
                    new_effects_this_scan = True

                    # Get enabling conditions via attribution
                    view = _HistoricalView(state)

                    def _eval(cond: Condition, _v: Any = view) -> bool:
                        return cond.evaluate(_v)  # type: ignore[arg-type]

                    attributions = attribute(sp_tree, _eval)
                    enabling: list[EnablingCondition] = []
                    for attr in attributions:
                        attr_tag = _condition_tag_name(attr.condition)
                        if attr_tag is None or attr_tag == cause_tag:
                            continue
                        held_since = _find_last_transition_scan(
                            history,
                            attr_tag,
                            current_scan,
                            timelines=timelines,
                            pdg=pdg,
                        )
                        enabling.append(
                            EnablingCondition(
                                tag_name=attr_tag,
                                value=state.tags.get(attr_tag),
                                held_since_scan=held_since,
                            )
                        )

                    steps.append(
                        ChainStep(
                            transition=effect_trans,
                            rung_index=rung_idx,
                            proximate_causes=(cause_trans,),
                            enabling_conditions=tuple(enabling),
                        )
                    )

                    # Add to frontier for further propagation
                    frontier[written_tag] = effect_trans

                # Only count first matching frontier tag per rung to avoid
                # duplicating steps.
                break

        if new_effects_this_scan:
            consecutive_empty = 0
        else:
            consecutive_empty += 1
            if consecutive_empty >= steady_state_k:
                break

    return CausalChain(
        effect=transition,
        mode="recorded",
        steps=steps,
    )


# ---------------------------------------------------------------------------
# Projected helpers
# ---------------------------------------------------------------------------


def _get_tag_name(tag: Tag | str) -> str:
    return tag if isinstance(tag, str) else tag.name


def _rung_writes_value_when_enabled(
    rung: Rung,
    tag_name: str,
    value: Any,
) -> bool:
    """Check if *rung* would write *value* to *tag_name* when its conditions are TRUE.

    Handles the three core coil types:
    - ``LatchInstruction`` → writes ``True``
    - ``ResetInstruction`` → writes ``tag.default`` (``False`` for Bool)
    - ``OutInstruction`` → writes ``True`` when enabled
    """
    from pyrung.core.instruction.coils import (
        LatchInstruction,
        OutInstruction,
        ResetInstruction,
    )
    from pyrung.core.tag import ImmediateRef
    from pyrung.core.tag import Tag as TagClass

    for instr in rung._instructions:
        target = getattr(instr, "target", None)
        if target is None:
            continue

        # Resolve ImmediateRef wrappers
        raw_target = target
        if isinstance(raw_target, ImmediateRef):
            raw_target = object.__getattribute__(raw_target, "value")

        if not isinstance(raw_target, TagClass):
            continue

        if raw_target.name != tag_name:
            continue

        if isinstance(instr, LatchInstruction):
            return value is True or value == True  # noqa: E712
        if isinstance(instr, ResetInstruction):
            return value == raw_target.default
        if isinstance(instr, OutInstruction):
            return value is True or value == True  # noqa: E712

    return False


# ---------------------------------------------------------------------------
# Projected backward walk
# ---------------------------------------------------------------------------


def _has_observed_transition(
    history: History,
    tag_name: str,
    to_value: Any,
    *,
    timelines: RungFiringTimelines | None = None,
    pdg: ProgramGraph | None = None,
) -> bool:
    """Check whether *tag_name* has ever transitioned to *to_value* in history."""
    ids = list(history.scan_ids())
    writers = _writer_indices(pdg, tag_name) if pdg is not None else None
    if timelines is not None and writers is not None and writers:
        for i in range(1, len(ids)):
            cur_val = _tag_value_at_scan(timelines, writers, tag_name, ids[i])
            if cur_val is _NO_WRITE:
                continue
            prev_val = _tag_value_at_scan(timelines, writers, tag_name, ids[i - 1])
            if prev_val is _NO_WRITE:
                prev_val = history.at(ids[i - 1]).tags.get(tag_name)
            if cur_val != prev_val and cur_val == to_value:
                return True
        return False

    # State-based fallback (also used for external-input tags with no writers)
    for i in range(1, len(ids)):
        cur_val = history.at(ids[i]).tags.get(tag_name)
        prev_val = history.at(ids[i - 1]).tags.get(tag_name)
        if cur_val != prev_val and cur_val == to_value:
            return True
    return False


def projected_cause(
    logic: list[Rung],
    history: History,
    tag: Tag | str,
    to_value: Any,
    pdg: ProgramGraph,
    assume: dict[str, Any] | None = None,
    *,
    timelines: RungFiringTimelines | None = None,
) -> CausalChain:
    """Build a projected causal chain: what would need to happen for *tag*
    to reach *to_value*?

    Walks the static PDG to find rungs that could write the desired value,
    then evaluates their SP trees against the current state to identify
    which conditions are already met (enabling) vs which need to transition
    (projected proximate causes).

    Returns ``mode='projected'`` when a reachable path exists, or
    ``mode='unreachable'`` with populated ``blockers`` when not.

    Args:
        logic: The program's rung list.
        history: The runner's History instance.
        tag: The tag (or tag name) to analyze.
        to_value: The desired target value.
        pdg: The program's static dependency graph.

    Returns:
        A ``CausalChain``.  Never returns ``None``.
    """
    tag_name = _get_tag_name(tag)

    latest_scan = history.newest_scan_id
    state = history.at(latest_scan)

    # Apply assumption overrides to the state snapshot
    if assume:
        state = state.with_tags(assume)

    current_value = state.tags.get(tag_name)

    # Hypothetical transition for the chain effect
    effect_transition = Transition(
        tag_name=tag_name,
        scan_id=latest_scan,
        from_value=current_value,
        to_value=to_value,
    )

    if current_value == to_value:
        # Already at desired value — projected with empty steps
        return CausalChain(effect=effect_transition, mode="projected")

    # Find rung indices that write to this tag (from PDG)
    writer_indices = pdg.writers_of.get(tag_name, frozenset())
    if not writer_indices:
        return CausalChain(
            effect=effect_transition,
            mode="unreachable",
            blockers=[
                BlockingCondition(
                    rung_index=-1,
                    blocked_tag=tag_name,
                    needed_value=to_value,
                    reason=BlockerReason.NO_OBSERVED_TRANSITION,
                )
            ],
        )

    # Find candidate rungs: those whose instructions would produce to_value
    candidate_rungs: list[tuple[int, Rung]] = []
    for node_idx in writer_indices:
        node = pdg.rung_nodes[node_idx]
        rung_idx = node.rung_index
        if rung_idx < len(logic):
            rung = logic[rung_idx]
            if _rung_writes_value_when_enabled(rung, tag_name, to_value):
                candidate_rungs.append((rung_idx, rung))

    if not candidate_rungs:
        return CausalChain(
            effect=effect_transition,
            mode="unreachable",
            blockers=[
                BlockingCondition(
                    rung_index=-1,
                    blocked_tag=tag_name,
                    needed_value=to_value,
                    reason=BlockerReason.NO_OBSERVED_TRANSITION,
                )
            ],
        )

    # Try each candidate rung, collect the best viable path and blockers
    best_steps: list[ChainStep] | None = None
    best_proximate: list[Transition] | None = None
    all_blockers: list[BlockingCondition] = []

    for rung_idx, rung in candidate_rungs:
        sp_tree = rung.sp_tree()

        if sp_tree is None:
            # Unconditional rung — trivially reachable
            steps = [
                ChainStep(
                    transition=effect_transition,
                    rung_index=rung_idx,
                    proximate_causes=(),
                    enabling_conditions=(),
                )
            ]
            if best_steps is None:
                best_steps = steps
                best_proximate = []
            continue

        # Collect ALL leaf conditions from the SP tree.  Unlike the
        # retrospective walk (which uses four-rule attribution to find
        # what mattered for the *current* evaluation), the projected walk
        # needs every contact because we're asking what would need to be
        # true for the rung to fire.
        view = _HistoricalView(state)

        def _eval(cond: Condition, _v: Any = view) -> bool:
            return cond.evaluate(_v)  # type: ignore[arg-type]

        leaves = _collect_sp_leaves(sp_tree)

        proximate: list[Transition] = []
        enabling: list[EnablingCondition] = []
        rung_blockers: list[BlockingCondition] = []
        seen_tags: set[str] = set()

        for leaf in leaves:
            cond_tag = _condition_tag_name(leaf.condition)
            if cond_tag is None or cond_tag in seen_tags:
                continue
            seen_tags.add(cond_tag)

            cond_value = state.tags.get(cond_tag)
            leaf_result = _eval(leaf.condition)

            if leaf_result:
                # Contact already evaluates TRUE → enabling
                enabling.append(
                    EnablingCondition(
                        tag_name=cond_tag,
                        value=cond_value,
                        held_since_scan=_find_last_transition_scan(
                            history, cond_tag, latest_scan + 1
                        ),
                    )
                )
            else:
                # Contact evaluates FALSE → needs to transition
                needed_value = not cond_value if cond_value is not None else True

                # Check reachability: has this tag ever transitioned to
                # the needed value in recorded history?  Tags in the
                # assume dict are reachable by stipulation.
                is_input = not pdg.writers_of.get(cond_tag, frozenset())
                reachable = (assume and cond_tag in assume) or _has_observed_transition(
                    history,
                    cond_tag,
                    needed_value,
                    timelines=timelines,
                    pdg=pdg,
                )

                if reachable or is_input:
                    proximate.append(
                        Transition(
                            tag_name=cond_tag,
                            scan_id=latest_scan,
                            from_value=cond_value,
                            to_value=needed_value,
                        )
                    )
                else:
                    reason = (
                        BlockerReason.NO_OBSERVED_TRANSITION
                        if is_input
                        else BlockerReason.BLOCKED_UPSTREAM
                    )
                    rung_blockers.append(
                        BlockingCondition(
                            rung_index=rung_idx,
                            blocked_tag=cond_tag,
                            needed_value=needed_value,
                            reason=reason,
                        )
                    )

        if not rung_blockers:
            # All conditions are reachable — viable path
            step = ChainStep(
                transition=effect_transition,
                rung_index=rung_idx,
                proximate_causes=tuple(proximate),
                enabling_conditions=tuple(enabling),
            )
            if best_steps is None or (
                best_proximate is not None and len(proximate) < len(best_proximate)
            ):
                best_steps = [step]
                best_proximate = proximate
        else:
            all_blockers.extend(rung_blockers)

    if best_steps is not None:
        return CausalChain(
            effect=effect_transition,
            mode="projected",
            steps=best_steps,
            conjunctive_roots=list(best_proximate or []),
        )

    # No viable path — unreachable
    return CausalChain(
        effect=effect_transition,
        mode="unreachable",
        blockers=all_blockers,
    )


# ---------------------------------------------------------------------------
# Projected forward walk
# ---------------------------------------------------------------------------


def projected_effect(
    logic: list[Rung],
    history: History,
    tag: Tag | str,
    from_value: Any,
    pdg: ProgramGraph,
    assume: dict[str, Any] | None = None,
) -> CausalChain:
    """Build a projected forward chain: what would happen if *tag*
    transitioned from *from_value*?

    Performs what-if analysis without mutating state. For Bool tags,
    the transition is ``from_value → not from_value``.

    Returns ``mode='projected'`` (possibly with empty steps for dead-end
    cases where nothing reads the tag), or ``mode='unreachable'`` if the
    trigger transition itself can't be reached.

    Args:
        logic: The program's rung list.
        history: The runner's History instance.
        tag: The tag (or tag name) to analyze.
        from_value: The value the tag would transition FROM (for Bool,
            the TO value is inferred as ``not from_value``).
        pdg: The program's static dependency graph.

    Returns:
        A ``CausalChain``.  Never returns ``None``.
    """
    tag_name = _get_tag_name(tag)

    # Infer the TO value (for Bool, flip)
    if isinstance(from_value, bool):
        to_value = not from_value
    else:
        # Non-Bool: can't infer TO — return unreachable
        return CausalChain(
            effect=Transition(tag_name, 0, from_value, from_value),
            mode="unreachable",
        )

    latest_scan = history.newest_scan_id
    state = history.at(latest_scan)

    # Apply assumption overrides to the state snapshot
    if assume:
        state = state.with_tags(assume)

    # Build the hypothetical transition
    cause_transition = Transition(
        tag_name=tag_name,
        scan_id=latest_scan,
        from_value=from_value,
        to_value=to_value,
    )

    # Check trigger reachability: is the from_→to_ transition itself
    # achievable? If the tag is at a different value than from_, the
    # trigger is not applicable from the current state.
    current_value = state.tags.get(tag_name)
    if current_value != from_value:
        # Trigger doesn't match current state — check if the trigger
        # is reachable via a projected cause walk
        trigger_chain = projected_cause(logic, history, tag, from_value, pdg, assume=assume)
        if trigger_chain.mode == "unreachable":
            return CausalChain(
                effect=cause_transition,
                mode="unreachable",
                blockers=trigger_chain.blockers,
            )

    steps: list[ChainStep] = []
    seen_effects: set[str] = {tag_name}

    # Frontier: tags whose effects we're tracing. Maps tag_name → Transition
    frontier: dict[str, Transition] = {tag_name: cause_transition}

    # Walk all rungs once per frontier expansion (single hypothetical scan)
    changed = True
    iterations = 0
    max_iterations = 10  # cap recursion depth for single-scan projection

    while changed and iterations < max_iterations:
        changed = False
        iterations += 1

        for rung_idx, rung in enumerate(logic):
            sp_tree = rung.sp_tree()
            if sp_tree is None:
                continue

            for cause_tag, cause_trans in list(frontier.items()):
                # Build views: hypothetical (with cause_tag at new value)
                # vs counterfactual (cause_tag at old value)
                hyp_view = _CounterfactualView(state, cause_tag, cause_trans.to_value)
                cf_view = _CounterfactualView(state, cause_tag, cause_trans.from_value)

                def _eval_hyp(cond: Condition, _v: Any = hyp_view) -> bool:
                    return cond.evaluate(_v)  # type: ignore[arg-type]

                def _eval_cf(cond: Condition, _v: Any = cf_view) -> bool:
                    return cond.evaluate(_v)  # type: ignore[arg-type]

                hyp_result = evaluate_sp(sp_tree, _eval_hyp)
                cf_result = evaluate_sp(sp_tree, _eval_cf)

                if hyp_result == cf_result:
                    continue  # cause_tag is not load-bearing for this rung

                # The transition changes this rung's outcome.
                # Determine what tags this rung writes.
                node = None
                for _ni, n in enumerate(pdg.rung_nodes):
                    if n.rung_index == rung_idx and not n.branch_path:
                        node = n
                        break

                if node is None:
                    continue

                for written_tag in node.writes:
                    if written_tag in seen_effects:
                        continue

                    seen_effects.add(written_tag)
                    changed = True

                    # Determine the hypothetical new value for the written tag
                    written_current = state.tags.get(written_tag)
                    written_new = _infer_written_value(rung, written_tag, hyp_result)
                    if written_new is None:
                        written_new = (
                            not written_current
                            if isinstance(written_current, bool)
                            else written_current
                        )

                    effect_trans = Transition(
                        tag_name=written_tag,
                        scan_id=latest_scan,
                        from_value=written_current,
                        to_value=written_new,
                    )

                    # Get enabling conditions via attribution
                    attributions = attribute(sp_tree, _eval_hyp)
                    enabling: list[EnablingCondition] = []
                    for attr in attributions:
                        attr_tag = _condition_tag_name(attr.condition)
                        if attr_tag is None or attr_tag == cause_tag:
                            continue
                        held_since = _find_last_transition_scan(history, attr_tag, latest_scan + 1)
                        enabling.append(
                            EnablingCondition(
                                tag_name=attr_tag,
                                value=state.tags.get(attr_tag),
                                held_since_scan=held_since,
                            )
                        )

                    steps.append(
                        ChainStep(
                            transition=effect_trans,
                            rung_index=rung_idx,
                            proximate_causes=(cause_trans,),
                            enabling_conditions=tuple(enabling),
                        )
                    )

                    frontier[written_tag] = effect_trans

                # Only count first matching frontier tag per rung
                break

    # Empty steps = dead-end (nothing reads the tag), still mode='projected'
    return CausalChain(
        effect=cause_transition,
        mode="projected",
        steps=steps,
    )


def _infer_written_value(rung: Rung, tag_name: str, rung_enabled: bool) -> Any | None:
    """Infer what value *rung* would write to *tag_name* given *rung_enabled*.

    Returns ``None`` if the value can't be determined (instruction doesn't
    write to this tag, or instruction type isn't recognized).
    """
    from pyrung.core.instruction.coils import (
        LatchInstruction,
        OutInstruction,
        ResetInstruction,
    )
    from pyrung.core.tag import ImmediateRef
    from pyrung.core.tag import Tag as TagClass

    for instr in rung._instructions:
        target = getattr(instr, "target", None)
        if target is None:
            continue

        raw_target = target
        if isinstance(raw_target, ImmediateRef):
            raw_target = object.__getattribute__(raw_target, "value")

        if not isinstance(raw_target, TagClass):
            continue

        if raw_target.name != tag_name:
            continue

        if isinstance(instr, LatchInstruction):
            return True if rung_enabled else None  # latch is no-op when disabled
        if isinstance(instr, ResetInstruction):
            return raw_target.default if rung_enabled else None
        if isinstance(instr, OutInstruction):
            return rung_enabled  # out writes True/False based on enabled

    return None
